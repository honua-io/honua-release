"""StudioAi and Bedrock calls through the canary client. This module does not edit that client."""

from __future__ import annotations

import sys
from pathlib import Path

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
    write_receipt,
)

from constants import ARMS


def apply_arm_temperature(payload: dict, temperature: float) -> dict:
    """The canary client pins temperature at 0. The arm setting is 0.7, so set it on the payload."""
    if payload.get("provider") == "bedrock" and "temperature" in payload:
        payload["temperature"] = temperature
    return payload


def dumps_with_arm_temperature(original, temperature: float):
    """Serialize like json.dumps, after writing the arm temperature onto a Bedrock request."""

    def dumps(obj, *args, **kwargs):
        if isinstance(obj, dict) and obj.get("provider") == "bedrock" and "temperature" in obj:
            apply_arm_temperature(obj, temperature)
        return original(obj, *args, **kwargs)

    return dumps


class OperateClient(CandidateProxyClient):
    """Same provenance checks as the canary client, with the arm temperature."""

    def __init__(self, config: EndpointConfig, *, temperature: float, **kwargs):
        super().__init__(config, **kwargs)
        self.temperature = temperature

    def complete(self, messages, certification):
        module = sys.modules[CandidateProxyClient.__module__]
        original = module.json.dumps
        module.json.dumps = dumps_with_arm_temperature(original, self.temperature)
        try:
            return super().complete(messages, certification)
        finally:
            module.json.dumps = original


def arm_temperature(label: str) -> float:
    return float(ARMS[label]["temperature"])


def build_endpoint(label: str) -> EndpointConfig:
    arm = ARMS[label]
    return EndpointConfig.from_environment(model=arm["bedrock"], runtime="aws-bedrock")


__all__ = [
    "CanaryError",
    "EndpointConfig",
    "ModelFailure",
    "OperateClient",
    "Redactor",
    "apply_arm_temperature",
    "arm_temperature",
    "dumps_with_arm_temperature",
    "build_endpoint",
    "parse_model_action",
    "write_receipt",
]
