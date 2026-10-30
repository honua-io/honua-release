"""Studio AI calls through the canary client. This module does not edit that client.

The canary request hardcodes temperature 0 and then requires the signed proxy bytes to
match that body. The customer proxy omits temperature, so Bedrock applies its default.
OperateClient.complete is the canary method with that one field removed. The signed
body therefore matches a request that omits temperature.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.terminal_model_canary import (  # noqa: E402
    CandidateProxyClient,
    CanaryError,
    EndpointConfig,
    ModelFailure,
    Redactor,
    parse_model_action,
    strict_json,
    write_receipt,
)

from constants import ARMS, SHIPPED_TEMPERATURE


class OperateClient(CandidateProxyClient):
    """Customer proxy request. Temperature is omitted on purpose."""

    def complete(
        self,
        messages: list[dict[str, str]],
        certification: dict[str, str],
    ) -> tuple[str, dict[str, Any], int, dict[str, Any]]:
        request_body = {
                "provider": "bedrock",
                "model": self.config.model,
                "messages": messages,
                "certification": certification,
            }
        payload = json.dumps(request_body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.config.api_key:
            if self.config.api_key_header == "x-api-key":
                headers["X-API-Key"] = self.config.api_key
            else:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
        manifest = self._manifest({key: value for key, value in headers.items() if key != "Content-Type"})
        request = urllib.request.Request(
            self.config.proxy_chat_url(), data=payload, headers=headers, method="POST"
        )
        started = time.monotonic()
        try:
            with self._open(request, timeout=self.timeout_seconds) as response:
                body = self._read(response).decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                problem = json.loads(exc.read(65536).decode("utf-8", "replace"))
                if isinstance(problem, dict):
                    fields = [str(problem.get(key)) for key in ("code", "errorCode", "title", "detail")
                              if isinstance(problem.get(key), (str, int)) and str(problem.get(key)).strip()]
                    detail = self.redactor.text(re.sub(r"\s+", " ", "; ".join(fields)))[:300]
            except (ValueError, OSError):
                detail = ""
            raise CanaryError(f"candidate Studio proxy request failed: {exc}"
                              + (f" ({detail})" if detail else "")) from exc
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
            raise CanaryError(f"candidate Studio proxy request failed: {exc}") from exc
        elapsed_ms = round((time.monotonic() - started) * 1000)
        event_types = {"message_start": "MessageStart", "text_delta": "TextDelta",
                       "tool_call_start": "ToolCallStart", "tool_call_delta": "ToolCallDelta",
                       "tool_call_stop": "ToolCallStop", "message_stop": "MessageStop",
                       "error": "Error", "transcript_provenance": "TranscriptProvenance"}
        events: list[tuple[str, dict[str, Any]]] = []
        event_name = ""
        data_lines: list[str] = []
        for line in [*body.splitlines(), ""]:
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif not line and event_name:
                try:
                    event = strict_json("\n".join(data_lines))
                except json.JSONDecodeError as exc:
                    raise CanaryError("candidate proxy emitted malformed SSE JSON") from exc
                if not isinstance(event, dict):
                    raise CanaryError("candidate proxy SSE data must be an object")
                if event_name not in event_types or event.get("type") != event_types[event_name]:
                    raise CanaryError("candidate proxy SSE event name/type disagree")
                events.append((event_name, event))
                event_name, data_lines = "", []
        provenance = [event for name, event in events if name == "transcript_provenance"]
        terminals = [name for name, _ in events if name in {"message_stop", "error"}]
        if (len(provenance) != 1 or len(terminals) != 1 or events[-1][0] != "transcript_provenance"
                or len(events) < 3 or events[0][0] != "message_start" or events[-2][0] != "message_stop"):
            reported = [event for name, event in events if name == "error"]
            detail = ""
            if reported:
                error = reported[0]
                code = re.sub(r"[^A-Za-z0-9_.:-]", "", str(error.get("errorCode") or "unspecified"))[:80] or "unspecified"
                message = self.redactor.text(re.sub(r"\s+", " ", str(error.get("errorMessage") or "")))[:240]
                detail = f"; proxy error {code}: {message}" if message else f"; proxy error {code}"
            elif not events:
                detail = "; the response carried no SSE events"
            raise CanaryError("candidate proxy omitted the unique terminal signed provenance event"
                              f" (events: {[name for name, _ in events][:12]}){detail}")
        if terminals[0] != "message_stop":
            raise CanaryError("candidate proxy ended the certified call with an error")
        signed = provenance[0].get("provenance")
        if not isinstance(signed, dict) or not signed.get("signature") or not signed.get("canonicalTranscript"):
            raise CanaryError("candidate proxy provenance envelope is incomplete")
        try:
            transcript = strict_json(base64.b64decode(signed["canonicalTranscript"], validate=True))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CanaryError("candidate proxy provenance envelope is malformed") from exc
        if not isinstance(transcript, dict):
            raise CanaryError("candidate signed transcript must be an object")
        if any(transcript.get(key) != value for key, value in certification.items()):
            raise CanaryError("candidate proxy provenance binding does not match the requested candidate action")
        if transcript.get("provider") != "bedrock" or not re.fullmatch(
                r"(?:(?:us|eu|apac|global)\.)?anthropic\.claude-[a-z0-9]+(?:[-:.][a-z0-9]+)*",
                str(transcript.get("model", ""))):
            raise CanaryError("promise journey requires signed Claude on Bedrock provenance")
        if transcript.get("model") != self.config.model:
            raise CanaryError("candidate proxy reported a model other than the requested model")
        provider_events = [event for name, event in events if name != "transcript_provenance"]
        verified_digest = self._verify_provenance(signed, transcript, manifest, request_body, provider_events)
        content = "".join(event.get("text", "") for name, event in events if name == "text_delta")
        if not isinstance(content, str) or not content.strip():
            raise ModelFailure("model response content is empty")
        stop = next(event for name, event in events if name == "message_stop")
        usage = {
            "prompt_tokens": stop.get("promptTokens", 0),
            "completion_tokens": stop.get("completionTokens", 0),
            "total_tokens": int(stop.get("promptTokens", 0) or 0) + int(stop.get("completionTokens", 0) or 0),
        }
        evidence = {
            "transcriptDigest": verified_digest,
            "keyId": signed["keyId"],
            "manifestDigest": hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "provider": transcript.get("provider"),
            "reportedModel": transcript.get("model"),
            "verifierVerdict": "pass",
            "replayVerdict": "consumed",
            "bindings": certification,
        }
        return content, usage, elapsed_ms, evidence


def build_endpoint(arm_id: str) -> EndpointConfig:
    arm = ARMS[arm_id]
    return EndpointConfig.from_environment(model=arm["bedrock"], runtime="aws-bedrock")


def shipped_temperature() -> int:
    return SHIPPED_TEMPERATURE


__all__ = [
    "CanaryError",
    "EndpointConfig",
    "ModelFailure",
    "OperateClient",
    "Redactor",
    "build_endpoint",
    "parse_model_action",
    "shipped_temperature",
    "write_receipt",
]
