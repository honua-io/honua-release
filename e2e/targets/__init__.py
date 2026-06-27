"""Deploy targets for the cross-cloud parity tier (docs/TEST-STRATEGY.md deploy-target matrix).

Each target provisions a real honua-server somewhere, exposes its public endpoint, and tears down.
The canonical parity set runs against that endpoint identically on every target. local-docker is the
reference (target #1); aws-serverless is the first cloud cell (Phase B, AWS-first).
"""
from __future__ import annotations

from .aws_serverless import AwsServerlessTarget
from .base import Availability, DeployTarget, ProvisionError

REGISTRY = {
    AwsServerlessTarget.name: AwsServerlessTarget,
}

__all__ = ["Availability", "DeployTarget", "ProvisionError", "AwsServerlessTarget", "REGISTRY"]
