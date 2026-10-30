"""StudioAi and Bedrock calls through the canary client. This module does not edit that client.

The shipped request temperature is the canary client's own value. This module does not rewrite it.
"""

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

from constants import ARMS, SHIPPED_TEMPERATURE

OperateClient = CandidateProxyClient


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
