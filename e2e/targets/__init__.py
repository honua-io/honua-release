"""Deploy targets for the cross-cloud parity tier (docs/TEST-STRATEGY.md deploy-target matrix).

Each target provisions a real honua-server somewhere, exposes its public endpoint, and tears down.
The canonical parity set runs against that endpoint identically on every target AND with Redis on/off,
so the platform is proven to behave the same everywhere and with/without its cache. local-docker is
the reference; the AWS cells (serverless / ECS / EKS) are the cloud tier (Phase B, AWS-first).
"""
from __future__ import annotations

from .aws_eks import AwsEksTarget
from .base import Availability, DeployTarget, ProvisionError
from .terraform_target import TerraformTarget, ecs, serverless

# name -> zero-arg-ish factory (accepts run_id/region kwargs).
REGISTRY = {
    "aws-serverless": serverless,
    "aws-ecs": ecs,
    "aws-eks": AwsEksTarget,
}

__all__ = ["Availability", "DeployTarget", "ProvisionError", "TerraformTarget", "AwsEksTarget", "REGISTRY"]
