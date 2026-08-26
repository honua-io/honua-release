#!/usr/bin/env python3
"""Run the harness for an OpenAI-compatible terminal-model canary.

The live journey adapter is owned by honua-release#123. Until that adapter is available, this tool
emits an honest skipped/blocked receipt and cannot claim model execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CANARY_DIR = ROOT / "certification" / "terminal-model-canary"
DEFAULT_JOURNEY = ROOT / "certification" / "terminal-journey" / "journey.v1.json"
DEFAULT_PROTOCOL = CANARY_DIR / "driver-protocol.v1.json"
DEFAULT_SCHEMA = CANARY_DIR / "receipt.schema.json"
DEFAULT_DRIVER = ROOT / "certification" / "terminal-journey" / "live_driver.py"
RECEIPT_SCHEMA = "terminal-model-canary-receipt-v1"
EVIDENCE_KEY = "release.e2e.terminal-model-canary"
MODEL_SELECTED = "MODEL_SELECTED"
HARNESS_DRIVEN = "HARNESS_DRIVEN"
MODEL_ACTION_KINDS = frozenset({"terminal_command", "tool_call"})
HARNESS_ACTION_KINDS = frozenset({"setup", "error_injection", "approval", "verification", "teardown"})
ASSERTION_NAMES = (
    "toolProfilePresent",
    "evidenceFresh",
    "authentication",
    "catalogExact",
    "fakeSuccessRejected",
    "rbacDenial",
    "tenantIsolation",
    "proposerApproverSeparation",
    "currentAuthorityRevalidation",
    "pixelProof",
    "finalUrlProof",
)


class CanaryError(RuntimeError):
    """Raised when the harness cannot produce trustworthy canary evidence."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanaryError(f"cannot read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CanaryError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise CanaryError(f"contract path must be inside the repository: {path}") from exc


def load_journey(path: Path) -> dict[str, Any]:
    """Load #123's stage contract without restating any stage in the canary."""
    journey = _load_json(path)
    stages = journey.get("stages")
    if not isinstance(stages, list) or not stages:
        raise CanaryError("#123 journey contract must contain at least one stage")
    numbers = [stage.get("number") for stage in stages if isinstance(stage, dict)]
    ids = [stage.get("id") for stage in stages if isinstance(stage, dict)]
    if len(numbers) != len(stages) or numbers != list(range(1, len(stages) + 1)):
        raise CanaryError("#123 journey stages must be objects numbered consecutively from one")
    if any(not isinstance(stage_id, str) or not stage_id for stage_id in ids) or len(set(ids)) != len(ids):
        raise CanaryError("#123 journey stage ids must be non-empty and unique")
    if any(not isinstance(stage.get("command"), str) or not stage["command"] for stage in stages):
        raise CanaryError("#123 journey stages must retain their reviewed command contract")
    return journey


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CanaryError(f"cannot read candidate manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("platformRelease"), str):
        raise CanaryError("candidate manifest must name platformRelease")
    if not isinstance(manifest.get("components"), dict) or not isinstance(
        manifest.get("clientArtifacts"), dict
    ):
        raise CanaryError("candidate manifest must contain components and clientArtifacts pins")
    return manifest


@dataclass(frozen=True)
class EndpointConfig:
    base_url: str | None
    model: str | None
    api_key: str | None
    api_key_env: str
    runtime: str | None
    quantization: str | None
    require_api_key: bool = False

    @classmethod
    def from_environment(
        cls,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key_env: str = "TERMINAL_MODEL_API_KEY",
        runtime: str | None = None,
        quantization: str | None = None,
        use_api_key: bool = True,
        require_api_key: bool = False,
    ) -> "EndpointConfig":
        return cls(
            base_url=(base_url if base_url is not None else os.getenv("TERMINAL_MODEL_BASE_URL")) or None,
            model=(model if model is not None else os.getenv("TERMINAL_MODEL_NAME")) or None,
            api_key=(os.getenv(api_key_env) or None) if use_api_key else None,
            api_key_env=api_key_env,
            runtime=(runtime if runtime is not None else os.getenv("TERMINAL_MODEL_RUNTIME")) or None,
            quantization=(
                quantization
                if quantization is not None
                else os.getenv("TERMINAL_MODEL_QUANTIZATION")
            )
            or None,
            require_api_key=require_api_key,
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def missing(self) -> list[str]:
        missing = []
        if not self.base_url:
            missing.append("TERMINAL_MODEL_BASE_URL")
        if not self.model:
            missing.append("TERMINAL_MODEL_NAME")
        return missing

    def validated_base_url(self) -> str:
        if not self.base_url:
            raise CanaryError("model endpoint base URL is absent")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CanaryError("model endpoint must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CanaryError("model endpoint URL must not contain credentials, query parameters, or fragments")
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))

    def chat_completions_url(self) -> str:
        base = self.validated_base_url()
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def evidence(self) -> dict[str, Any]:
        base_url = self.validated_base_url() if self.base_url else None
        return {
            "configured": self.configured,
            "baseUrl": base_url,
            "model": self.model,
            "runtime": self.runtime,
            "quantization": self.quantization,
            "authentication": {
                "mode": "bearer-env" if self.api_key else "none",
                "credentialReference": f"env:{self.api_key_env}" if self.api_key else None,
                "required": self.require_api_key,
            },
        }


class Redactor:
    """Remove credential material before data enters a prompt, transcript, or receipt."""

    _patterns = (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']+"),
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|password|secret|access[_-]?token|token)[\"']?"
            r"\s*[:=]\s*[\"']?)[^\s,;\"']+"
        ),
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    )
    _sensitive_key = re.compile(r"(?i)(authorization|api[_-]?key|password|secret|token)")

    def __init__(self, sensitive_values: list[str] | None = None):
        self._sensitive = sorted(
            {value for value in (sensitive_values or []) if isinstance(value, str) and value},
            key=len,
            reverse=True,
        )

    def text(self, value: str) -> str:
        redacted = value
        for secret in self._sensitive:
            redacted = redacted.replace(secret, "[REDACTED]")
        redacted = self._patterns[0].sub(r"\1[REDACTED]", redacted)
        redacted = self._patterns[1].sub(r"\1[REDACTED]", redacted)
        redacted = self._patterns[2].sub("[REDACTED]", redacted)
        return redacted

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                name = str(key)
                if self._sensitive_key.search(name) and "reference" not in name.lower():
                    redacted[name] = "[REDACTED]"
                else:
                    redacted[name] = self.value(item)
            return redacted
        return value


class ReceiptBuilder:
    def __init__(
        self,
        *,
        manifest: dict[str, Any],
        journey: dict[str, Any],
        journey_path: Path,
        protocol: dict[str, Any],
        protocol_path: Path,
        endpoint: EndpointConfig,
        driver_command: Path | None,
        injection_stage: str,
        deterministic_receipt: str | None,
        redactor: Redactor,
    ):
        self.journey = journey
        self.protocol = protocol
        self.redactor = redactor
        self._stages_by_id = {stage["id"]: stage for stage in journey["stages"]}
        if injection_stage not in self._stages_by_id:
            raise CanaryError(f"injection stage {injection_stage!r} is not in #123's journey contract")
        self.receipt: dict[str, Any] = {
            "schemaVersion": 1,
            "receiptSchema": RECEIPT_SCHEMA,
            "evidenceKey": EVIDENCE_KEY,
            "generatedAt": _now(),
            "status": "blocked",
            "scope": {
                "delivery": "harness-only",
                "executionToGreen": "blocked",
                "blockedBy": ["honua-release#123"],
            },
            "journeyContract": {
                "path": _repo_path(journey_path),
                "sha256": _sha256(journey_path),
                "schemaVersion": journey.get("schemaVersion"),
                "evidenceKey": journey.get("evidenceKey"),
                "receiptSchema": journey.get("receiptSchema"),
            },
            "candidate": {
                "platformRelease": manifest["platformRelease"],
                "components": json.loads(json.dumps(manifest["components"])),
                "clientArtifacts": json.loads(json.dumps(manifest["clientArtifacts"])),
            },
            "endpoint": endpoint.evidence(),
            "driver": {
                "configured": driver_command is not None and driver_command.is_file(),
                "command": _repo_path(driver_command) if driver_command is not None else None,
                "protocol": protocol.get("protocol"),
                "protocolPath": _repo_path(protocol_path),
                "protocolSha256": _sha256(protocol_path),
                "owner": protocol.get("owner"),
            },
            "stages": [
                {
                    "number": stage["number"],
                    "id": stage["id"],
                    "status": "not-run",
                    "modelActionSequences": [],
                    "harnessActionSequences": [],
                    "blockedBy": list(stage.get("blockedBy", [])),
                }
                for stage in journey["stages"]
            ],
            "actions": [],
            "transcript": {"redacted": True, "entries": []},
            "errorInjection": {
                "id": "recoverable-error-1",
                "stageId": injection_stage,
                "injectedBy": HARNESS_DRIVEN,
                "recoverable": True,
                "status": "not-run",
                "injectionActionSequence": None,
                "triggeringModelActionSequence": None,
                "recoveryModelActionSequence": None,
            },
            "totals": {
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "modelCalls": 0,
                "elapsedMs": 0,
            },
            "assertions": {name: "not-run" for name in ASSERTION_NAMES},
            "notices": [],
            "linkedEvidence": {
                "deterministicJourney": "honua-release#123",
                "deterministicReceipt": deterministic_receipt,
            },
        }

    def _stage_record(self, stage_id: str) -> dict[str, Any]:
        try:
            return next(stage for stage in self.receipt["stages"] if stage["id"] == stage_id)
        except StopIteration as exc:
            raise CanaryError(f"unknown journey stage {stage_id!r}") from exc

    def capture_transcript(self, role: str, content: Any, *, stage_id: str | None) -> int:
        if role not in {"system", "user", "assistant", "driver"}:
            raise CanaryError(f"unsupported transcript role {role!r}")
        sequence = len(self.receipt["transcript"]["entries"]) + 1
        self.receipt["transcript"]["entries"].append(
            {
                "sequence": sequence,
                "stageId": stage_id,
                "role": role,
                "content": self.redactor.value(content),
            }
        )
        return sequence

    def record_action(
        self,
        *,
        stage_id: str,
        attribution: str,
        kind: str,
        status: str,
        request: Any,
        result: Any,
        transcript_sequence: int | None = None,
    ) -> int:
        stage = self._stages_by_id.get(stage_id)
        if stage is None:
            raise CanaryError(f"unknown journey stage {stage_id!r}")
        if attribution == MODEL_SELECTED:
            if kind not in MODEL_ACTION_KINDS:
                raise CanaryError("MODEL_SELECTED attribution is reserved for commands and tool calls")
            entries = self.receipt["transcript"]["entries"]
            if transcript_sequence is None or not any(
                entry["sequence"] == transcript_sequence and entry["role"] == "assistant"
                for entry in entries
            ):
                raise CanaryError("a model-selected action must reference its assistant transcript entry")
            selection = {"transcriptSequence": transcript_sequence}
        elif attribution == HARNESS_DRIVEN:
            if kind not in HARNESS_ACTION_KINDS:
                raise CanaryError("HARNESS_DRIVEN attribution cannot claim a model command or tool call")
            if transcript_sequence is not None:
                raise CanaryError("harness-driven actions cannot cite model selection evidence")
            selection = None
        else:
            raise CanaryError(f"unknown action attribution {attribution!r}")
        if status not in {"pass", "fail", "blocked", "pending"}:
            raise CanaryError(f"unknown action status {status!r}")

        sequence = len(self.receipt["actions"]) + 1
        self.receipt["actions"].append(
            {
                "sequence": sequence,
                "stageNumber": stage["number"],
                "stageId": stage_id,
                "attribution": attribution,
                "kind": kind,
                "status": status,
                "request": self.redactor.value(request),
                "result": self.redactor.value(result),
                "selectionEvidence": selection,
            }
        )
        receipt_stage = self._stage_record(stage_id)
        key = "modelActionSequences" if attribution == MODEL_SELECTED else "harnessActionSequences"
        receipt_stage[key].append(sequence)
        return sequence

    def arm_injection(self, *, stage_id: str, result: Any) -> int:
        injection = self.receipt["errorInjection"]
        if injection["status"] != "not-run" or stage_id != injection["stageId"]:
            raise CanaryError("recoverable error must be armed exactly once at its declared stage")
        sequence = self.record_action(
            stage_id=stage_id,
            attribution=HARNESS_DRIVEN,
            kind="error_injection",
            status="pass",
            request={"errorId": injection["id"], "recoverable": True, "once": True},
            result=result,
        )
        injection["status"] = "armed"
        injection["injectionActionSequence"] = sequence
        return sequence

    def observe_injected_error(self, model_action_sequence: int) -> None:
        injection = self.receipt["errorInjection"]
        if injection["status"] != "armed" or not self._is_model_action(model_action_sequence):
            raise CanaryError("injected error observation must bind the armed error to a model action")
        injection["status"] = "observed"
        injection["triggeringModelActionSequence"] = model_action_sequence

    def record_recovery(self, model_action_sequence: int, recovered_error: Any) -> None:
        injection = self.receipt["errorInjection"]
        if injection["status"] != "observed" or not self._is_model_action(model_action_sequence):
            raise CanaryError("error recovery must be a later model-selected action")
        if model_action_sequence == injection["triggeringModelActionSequence"]:
            raise CanaryError("the action that observed the injected error cannot also prove recovery")
        if (
            not isinstance(recovered_error, dict)
            or recovered_error.get("id") != injection["id"]
            or recovered_error.get("recovered") is not True
        ):
            raise CanaryError("model action result did not prove recovery of the injected error")
        injection["status"] = "recovered"
        injection["recoveryModelActionSequence"] = model_action_sequence

    def _is_model_action(self, sequence: int) -> bool:
        return any(
            action["sequence"] == sequence and action["attribution"] == MODEL_SELECTED
            for action in self.receipt["actions"]
        )

    def mark_stage(self, stage_id: str, status: str) -> None:
        self._stage_record(stage_id)["status"] = status

    def mark_skipped(self, why: str) -> None:
        self.receipt["status"] = "skipped"
        self.receipt["notices"].append(why)
        for stage in self.receipt["stages"]:
            stage["status"] = "skipped"

    def mark_blocked(self, why: str) -> None:
        self.receipt["status"] = "blocked"
        self.receipt["notices"].append(why)
        for stage in self.receipt["stages"]:
            stage["status"] = "blocked"

    def mark_failed(self, why: str) -> None:
        self.receipt["status"] = "fail"
        self.receipt["notices"].append(why)
        for stage in self.receipt["stages"]:
            if stage["status"] == "running":
                stage["status"] = "fail"
            elif stage["status"] == "not-run":
                stage["status"] = "blocked"
        if self.receipt["errorInjection"]["status"] in {"armed", "observed"}:
            self.receipt["errorInjection"]["status"] = "failed"

    def add_usage(self, usage: dict[str, Any], elapsed_ms: int) -> None:
        totals = self.receipt["totals"]
        totals["promptTokens"] += int(usage.get("prompt_tokens") or 0)
        totals["completionTokens"] += int(usage.get("completion_tokens") or 0)
        totals["totalTokens"] += int(usage.get("total_tokens") or 0)
        totals["modelCalls"] += 1
        totals["elapsedMs"] += elapsed_ms

    def validate_invariants(self) -> None:
        transcript = {entry["sequence"]: entry for entry in self.receipt["transcript"]["entries"]}
        for action in self.receipt["actions"]:
            if action["attribution"] == MODEL_SELECTED:
                evidence = action["selectionEvidence"]
                selected = transcript.get(evidence["transcriptSequence"] if evidence else None)
                if selected is None or selected["role"] != "assistant":
                    raise CanaryError("model action attribution is not backed by an assistant transcript")
            elif action["selectionEvidence"] is not None:
                raise CanaryError("harness action carries false model-selection evidence")

        if self.receipt["status"] == "pass":
            if not self.receipt["endpoint"]["configured"] or not self.receipt["driver"]["configured"]:
                raise CanaryError("a pass requires both model endpoint and #123 driver")
            if self.receipt["scope"]["executionToGreen"] != "pass":
                raise CanaryError("a pass must explicitly mark execution-to-green complete")
            if not self.receipt["linkedEvidence"]["deterministicReceipt"]:
                raise CanaryError("a pass requires the green deterministic #123 receipt")
            if not self.receipt["endpoint"]["runtime"] or not self.receipt["endpoint"]["quantization"]:
                raise CanaryError("a pass requires explicit model runtime and quantization identifiers")
            if any(
                stage["status"] != "pass" or not stage["modelActionSequences"]
                for stage in self.receipt["stages"]
            ):
                raise CanaryError("every imported #123 stage needs a model-selected action and pass")
            if self.receipt["errorInjection"]["status"] != "recovered":
                raise CanaryError("a pass requires observed model recovery from the injected error")
            if any(value != "pass" for value in self.receipt["assertions"].values()):
                raise CanaryError("a pass requires every security and proof assertion")

    def validated_receipt(self, schema: dict[str, Any]) -> dict[str, Any]:
        self.validate_invariants()
        errors = sorted(Draft202012Validator(schema).iter_errors(self.receipt), key=lambda error: list(error.path))
        if errors:
            first = errors[0]
            where = ".".join(str(part) for part in first.path) or "receipt"
            raise CanaryError(f"receipt schema validation failed at {where}: {first.message}")
        return self.receipt


class OpenAICompatibleClient:
    """Small dependency-free Chat Completions client for hosted or local compatible endpoints."""

    def __init__(self, config: EndpointConfig, *, timeout_seconds: int = 120):
        if not config.configured:
            raise CanaryError("model endpoint configuration is incomplete")
        self.config = config
        self.timeout_seconds = timeout_seconds

    def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any], int]:
        payload = json.dumps(
            {"model": self.config.model, "messages": messages, "temperature": 0, "stream": False}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(
            self.config.chat_completions_url(), data=payload, headers=headers, method="POST"
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise CanaryError(f"OpenAI-compatible model request failed: {exc}") from exc
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if not isinstance(result, dict):
            raise CanaryError("model response must be a JSON object")
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CanaryError("model response does not contain choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise CanaryError("model response content is empty")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return content, usage, elapsed_ms


class DriverAdapter:
    """Invoke the future #123 live driver through the reviewed JSON protocol."""

    def __init__(self, command: Path, protocol: dict[str, Any], *, timeout_seconds: int = 300):
        if not command.is_file():
            raise CanaryError(f"#123 driver command does not exist: {command}")
        self.argv = [sys.executable, str(command)] if command.suffix.lower() == ".py" else [str(command)]
        operations = protocol.get("operations")
        if not isinstance(operations, dict) or not operations:
            raise CanaryError("#123 driver protocol has no operations")
        self.operations = operations
        self.timeout_seconds = timeout_seconds

    def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        operation_contract = self.operations.get(operation)
        if not isinstance(operation_contract, dict):
            raise CanaryError(f"operation {operation!r} is not declared by the #123 driver protocol")
        request = {"protocol": "terminal-journey-driver-v1", "operation": operation, **payload}
        completed = subprocess.run(
            self.argv,
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise CanaryError(f"#123 driver {operation} failed with exit code {completed.returncode}")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CanaryError(f"#123 driver {operation} returned non-JSON output") from exc
        if not isinstance(response, dict):
            raise CanaryError(f"#123 driver {operation} response must be an object")
        required = operation_contract.get("requiredResponse")
        if not isinstance(required, list) or any(field not in response for field in required):
            missing = [field for field in required or [] if field not in response]
            raise CanaryError(f"#123 driver {operation} response omitted required fields: {missing}")
        return response


def parse_model_action(content: str) -> tuple[str, dict[str, Any]]:
    try:
        action = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CanaryError("model did not select an action as JSON") from exc
    if not isinstance(action, dict) or action.get("kind") not in MODEL_ACTION_KINDS:
        raise CanaryError("model action must be a terminal_command or tool_call object")
    kind = action["kind"]
    if kind == "terminal_command" and (
        not isinstance(action.get("command"), str) or not action["command"].strip()
    ):
        raise CanaryError("model-selected terminal_command must contain a command")
    if kind == "tool_call" and (
        not isinstance(action.get("tool"), str)
        or not action["tool"].strip()
        or not isinstance(action.get("arguments"), dict)
    ):
        raise CanaryError("model-selected tool_call must contain a tool and arguments object")
    return kind, action


def build_receipt_builder(
    *,
    manifest_path: Path,
    journey_path: Path,
    protocol_path: Path,
    endpoint: EndpointConfig,
    driver_command: Path | None,
    injection_stage: str | None = None,
    deterministic_receipt: str | None = None,
) -> ReceiptBuilder:
    manifest = load_manifest(manifest_path)
    journey = load_journey(journey_path)
    protocol = _load_json(protocol_path)
    if protocol.get("protocol") != "terminal-journey-driver-v1" or protocol.get("owner") != "honua-release#123":
        raise CanaryError("driver protocol ownership or version drifted from #123")
    if driver_command is not None and _repo_path(driver_command) != protocol.get("adapterPath"):
        raise CanaryError("driver command must be #123's declared repository adapter path")
    selected_stage = injection_stage or journey["stages"][min(3, len(journey["stages"]) - 1)]["id"]
    return ReceiptBuilder(
        manifest=manifest,
        journey=journey,
        journey_path=journey_path,
        protocol=protocol,
        protocol_path=protocol_path,
        endpoint=endpoint,
        driver_command=driver_command,
        injection_stage=selected_stage,
        deterministic_receipt=deterministic_receipt,
        redactor=Redactor([endpoint.api_key] if endpoint.api_key else []),
    )


def unavailable_receipt(
    builder: ReceiptBuilder,
    endpoint: EndpointConfig,
    driver_command: Path | None,
) -> dict[str, Any]:
    if not endpoint.configured:
        builder.mark_skipped(
            "OpenAI-compatible endpoint configuration is absent "
            f"(missing {', '.join(endpoint.missing)}); canary execution was skipped, never passed"
        )
    elif endpoint.require_api_key and not endpoint.api_key:
        builder.mark_skipped(
            f"hosted bearer authentication requires env:{endpoint.api_key_env}, but it is absent; "
            "canary execution was skipped, never passed"
        )
    elif driver_command is None or not driver_command.is_file():
        builder.mark_blocked(
            "harness is ready, but #123 has not supplied a live terminal-journey-driver-v1 adapter "
            "green against the candidate"
        )
    elif not builder.receipt["linkedEvidence"]["deterministicReceipt"]:
        builder.mark_blocked("live execution requires the green deterministic #123 receipt URI")
    elif not endpoint.runtime or not endpoint.quantization:
        builder.mark_blocked("live execution requires explicit model runtime and quantization identifiers")
    else:
        raise CanaryError("unavailable_receipt called with a runnable endpoint and driver")
    return builder.receipt


def _system_contract() -> str:
    return (
        "Operate the bounded Honua terminal journey from observed state. Select exactly one real "
        "terminal command or server-authored tool call per response. Never claim success from prose, "
        "a fake token, a replay, or an unexecuted write. Stop at approval; the harness supplies the "
        "separate human approval. Respond only as JSON: either "
        '{"kind":"terminal_command","command":"...","intent":"..."} or '
        '{"kind":"tool_call","tool":"...","arguments":{},"intent":"..."}.'
    )


def execute_live(
    builder: ReceiptBuilder,
    *,
    endpoint: EndpointConfig,
    driver_command: Path,
    max_actions_per_stage: int,
) -> dict[str, Any]:
    """Execute when #123 eventually supplies its live adapter; otherwise callers never enter here."""
    client = OpenAICompatibleClient(endpoint)
    driver = DriverAdapter(driver_command, builder.protocol)
    receipt = builder.receipt
    receipt["scope"]["executionToGreen"] = "attempted"
    receipt["scope"]["blockedBy"] = []
    first_stage = builder.journey["stages"][0]["id"]
    last_stage = builder.journey["stages"][-1]["id"]
    workspace_id: str | None = None
    approval_done = False
    try:
        setup_request = {
            "candidate": receipt["candidate"],
            "journeyContract": receipt["journeyContract"],
        }
        setup = builder.redactor.value(driver.invoke("setup", setup_request))
        if (
            setup.get("status") != "ready"
            or not isinstance(setup.get("workspaceId"), str)
            or not setup.get("toolView")
            or not setup.get("credentialReferences")
        ):
            raise CanaryError("#123 driver setup did not return a ready clean workspace")
        workspace_id = setup["workspaceId"]
        builder.record_action(
            stage_id=first_stage,
            attribution=HARNESS_DRIVEN,
            kind="setup",
            status="pass",
            request=setup_request,
            result=setup,
        )
        builder.capture_transcript("driver", setup, stage_id=first_stage)

        for stage in builder.journey["stages"]:
            stage_id = stage["id"]
            builder.mark_stage(stage_id, "running")
            if stage_id == receipt["errorInjection"]["stageId"]:
                injection_response = builder.redactor.value(
                    driver.invoke(
                        "inject_error",
                        {
                            "workspaceId": workspace_id,
                            "stage": stage,
                            "errorId": receipt["errorInjection"]["id"],
                            "recoverable": True,
                            "once": True,
                        },
                    )
                )
                if (
                    injection_response.get("status") != "armed"
                    or injection_response.get("errorId") != receipt["errorInjection"]["id"]
                    or injection_response.get("recoverable") is not True
                ):
                    raise CanaryError("#123 driver did not confirm the recoverable error injection")
                builder.arm_injection(stage_id=stage_id, result=injection_response)

            for _ in range(max_actions_per_stage):
                observed = builder.redactor.value(
                    driver.invoke("observe", {"workspaceId": workspace_id, "stage": stage})
                )
                builder.capture_transcript("driver", observed, stage_id=stage_id)
                stage_status = observed.get("stageStatus")
                if stage_status == "awaiting_approval":
                    if approval_done:
                        raise CanaryError("driver requested more than one approval boundary")
                    approval_request = {
                        "workspaceId": workspace_id,
                        "stage": stage,
                        "proposalId": observed.get("proposalId"),
                        "principalProfileReference": "profile:approver",
                    }
                    approval = builder.redactor.value(driver.invoke("approve", approval_request))
                    if (
                        approval.get("status") != "approved"
                        or approval.get("proposerSelfApproval") != "denied"
                    ):
                        raise CanaryError("separate-principal approval or proposer denial was not proved")
                    builder.record_action(
                        stage_id=stage_id,
                        attribution=HARNESS_DRIVEN,
                        kind="approval",
                        status="pass",
                        request=approval_request,
                        result=approval,
                    )
                    approval_done = True
                    continue
                if stage_status == "complete":
                    if not builder._stage_record(stage_id)["modelActionSequences"]:
                        raise CanaryError(f"stage {stage_id} completed without a model-selected action")
                    builder.mark_stage(stage_id, "pass")
                    break
                if stage_status != "ready":
                    raise CanaryError(f"stage {stage_id} returned untrusted status {stage_status!r}")

                system = _system_contract()
                user_content = json.dumps(
                    {
                        "task": "advance exactly this imported #123 stage from observed state",
                        "candidateRelease": receipt["candidate"]["platformRelease"],
                        "stage": stage,
                        "observation": observed.get("observation"),
                        "serverAuthoredToolView": observed.get("toolView"),
                    },
                    separators=(",", ":"),
                )
                builder.capture_transcript("system", system, stage_id=stage_id)
                builder.capture_transcript("user", user_content, stage_id=stage_id)
                content, usage, elapsed_ms = client.complete(
                    [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
                )
                assistant_sequence = builder.capture_transcript("assistant", content, stage_id=stage_id)
                builder.add_usage(usage, elapsed_ms)
                kind, model_action = parse_model_action(content)
                execution = builder.redactor.value(
                    driver.invoke(
                        "execute",
                        {"workspaceId": workspace_id, "stage": stage, "action": model_action},
                    )
                )
                builder.capture_transcript("driver", execution, stage_id=stage_id)
                action_status = "pass" if execution.get("status") in {"ok", "pass"} else "fail"
                action_sequence = builder.record_action(
                    stage_id=stage_id,
                    attribution=MODEL_SELECTED,
                    kind=kind,
                    status=action_status,
                    request=model_action,
                    result=execution,
                    transcript_sequence=assistant_sequence,
                )
                injected = execution.get("injectedError")
                if isinstance(injected, dict) and injected.get("id") == receipt["errorInjection"]["id"]:
                    if injected.get("recoverable") is not True:
                        raise CanaryError("driver reported the injected error as non-recoverable")
                    builder.observe_injected_error(action_sequence)
                elif receipt["errorInjection"]["status"] == "observed" and action_status == "pass":
                    recovered = execution.get("recoveredError")
                    if (
                        not isinstance(recovered, dict)
                        or recovered.get("id") != receipt["errorInjection"]["id"]
                        or recovered.get("recovered") is not True
                    ):
                        raise CanaryError(
                            "a later successful action did not prove recovery of the injected error"
                        )
                    builder.record_recovery(action_sequence, recovered)
            else:
                raise CanaryError(
                    f"stage {stage_id} exceeded the bounded limit of {max_actions_per_stage} model actions"
                )

        if not approval_done:
            raise CanaryError("journey never reached the separate-principal approval boundary")
        if receipt["errorInjection"]["status"] != "recovered":
            raise CanaryError("model did not observe and recover from the injected error")
        verification_request = {"workspaceId": workspace_id, "candidate": receipt["candidate"]}
        verification = builder.redactor.value(driver.invoke("verify", verification_request))
        assertions = verification.get("assertions")
        if verification.get("status") != "pass" or not isinstance(assertions, dict):
            raise CanaryError("final #123 verification did not pass")
        if (
            not verification.get("finalUrlProof")
            or not verification.get("pixelProof")
            or not verification.get("canonicalIds")
        ):
            raise CanaryError("final #123 verification omitted canonical IDs, pixel proof, or final-URL proof")
        for name in ASSERTION_NAMES:
            receipt["assertions"][name] = "pass" if assertions.get(name) == "pass" else "fail"
        builder.record_action(
            stage_id=last_stage,
            attribution=HARNESS_DRIVEN,
            kind="verification",
            status="pass" if all(value == "pass" for value in receipt["assertions"].values()) else "fail",
            request=verification_request,
            result=verification,
        )
        if any(value != "pass" for value in receipt["assertions"].values()):
            raise CanaryError("one or more required final assertions failed")
        receipt["status"] = "pass"
        receipt["scope"]["executionToGreen"] = "pass"
    except (CanaryError, subprocess.TimeoutExpired) as exc:
        builder.mark_failed(str(exc))
    finally:
        if workspace_id:
            try:
                teardown_request = {"workspaceId": workspace_id}
                teardown = builder.redactor.value(driver.invoke("teardown", teardown_request))
                builder.record_action(
                    stage_id=last_stage,
                    attribution=HARNESS_DRIVEN,
                    kind="teardown",
                    status="pass" if teardown.get("status") == "complete" else "fail",
                    request=teardown_request,
                    result=teardown,
                )
                if teardown.get("status") != "complete":
                    builder.mark_failed("isolated workspace teardown did not complete")
            except (CanaryError, subprocess.TimeoutExpired) as exc:
                builder.mark_failed(f"isolated workspace teardown failed: {exc}")
    return receipt


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--journey", type=Path, default=DEFAULT_JOURNEY)
    parser.add_argument("--driver-protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--receipt-schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--runtime")
    parser.add_argument("--quantization")
    parser.add_argument("--api-key-env", default="TERMINAL_MODEL_API_KEY")
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument("--no-api-key", action="store_true")
    authentication.add_argument("--require-api-key", action="store_true")
    parser.add_argument("--driver-command", type=Path, default=DEFAULT_DRIVER)
    parser.add_argument("--deterministic-receipt")
    parser.add_argument("--inject-stage")
    parser.add_argument("--max-actions-per-stage", type=int, default=12)
    args = parser.parse_args(argv)
    if args.max_actions_per_stage <= 0:
        parser.error("--max-actions-per-stage must be positive")

    endpoint = EndpointConfig.from_environment(
        base_url=args.base_url,
        model=args.model,
        api_key_env=args.api_key_env,
        runtime=args.runtime,
        quantization=args.quantization,
        use_api_key=not args.no_api_key,
        require_api_key=args.require_api_key,
    )
    try:
        builder = build_receipt_builder(
            manifest_path=args.manifest,
            journey_path=args.journey,
            protocol_path=args.driver_protocol,
            endpoint=endpoint,
            driver_command=args.driver_command,
            injection_stage=args.inject_stage,
            deterministic_receipt=args.deterministic_receipt,
        )
        if (
            not endpoint.configured
            or (endpoint.require_api_key and not endpoint.api_key)
            or args.driver_command is None
            or not args.driver_command.is_file()
            or not args.deterministic_receipt
            or not endpoint.runtime
            or not endpoint.quantization
        ):
            unavailable_receipt(builder, endpoint, args.driver_command)
        else:
            execute_live(
                builder,
                endpoint=endpoint,
                driver_command=args.driver_command,
                max_actions_per_stage=args.max_actions_per_stage,
            )
        schema = _load_json(args.receipt_schema)
        receipt = builder.validated_receipt(schema)
        write_receipt(args.output, receipt)
        status = receipt["status"]
        if status == "skipped":
            print(
                "::notice title=Terminal model canary skipped::Required endpoint or hosted "
                "credential configuration is absent; see the redacted receipt"
            )
            print("terminal model canary: skipped")
            return 0
        if status == "blocked":
            print(
                "::notice title=Terminal model canary blocked::A required live dependency is "
                "absent; see the redacted receipt"
            )
            print("terminal model canary: blocked")
            return 0
        if status == "pass":
            print("terminal model canary: pass")
            return 0
        print("terminal model canary: fail")
        return 1
    except CanaryError as exc:
        print(f"terminal model canary harness error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
