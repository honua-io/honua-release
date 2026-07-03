"""Config-driven Terraform deploy target — the shared driver for the AWS cells whose app endpoint is a
direct terraform output (serverless: Lambda+API GW; ECS: Fargate+ALB).

Each cell terraform-applies one honua-iac example root with the candidate image + the Redis toggle,
reads the `honua_url` output, and (the caller runs the canonical set) then destroys — ephemeral,
run-scoped, reaper-on-teardown. BLOCKED (honest, not green) until terraform + AWS creds + a deployable
image + the honua-iac tree are all present.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .base import Availability, DeployTarget, ProvisionError


@dataclass(frozen=True)
class TfTargetSpec:
    name: str
    root: str            # path under the honua-iac tree, e.g. infrastructure/terraform/examples/aws
    image_env: str       # env var holding the deployable image (differs: ECR Lambda image vs ECS image)
    image_var: str       # the terraform variable that takes the image (honua_image_uri vs honua_image)
    endpoint_output: str = "honua_url"
    redis_var: str = "redis_enabled"
    image_hint: str = ""  # human hint for the BLOCKED message


class TerraformTarget(DeployTarget):
    supports_redis = True

    def __init__(self, spec: TfTargetSpec, *, run_id: str = "local", region: str | None = None) -> None:
        self.spec = spec
        self.name = spec.name
        self.run_id = run_id
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._workdir: Path | None = None
        self._last_vars: list[str] = []

    # --- prerequisites -------------------------------------------------------------------------
    def _iac_root(self) -> Path | None:
        base = os.environ.get("HONUA_IAC_DIR")
        if not base:
            return None
        root = Path(base) / self.spec.root
        return root if root.is_dir() else None

    @staticmethod
    def _has_aws_creds() -> bool:
        return bool(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ROLE_ARN")
                    or os.environ.get("AWS_PROFILE") or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"))

    def availability(self) -> Availability:
        missing: list[str] = []
        if not shutil.which("terraform"):
            missing.append("terraform CLI")
        if not self._has_aws_creds():
            missing.append("AWS credentials (OIDC role / AWS_* env)")
        if not os.environ.get(self.spec.image_env):
            missing.append(f"{self.spec.image_env} ({self.spec.image_hint or 'deployable image'})")
        if self._iac_root() is None:
            missing.append("HONUA_IAC_DIR pointing at the honua-iac terraform tree")
        if missing:
            return Availability(False, f"{self.name} not runnable: " + "; ".join(missing), missing)
        return Availability(True, f"{self.name} prerequisites present")

    # --- terraform lifecycle -------------------------------------------------------------------
    def _tf(self, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["terraform", f"-chdir={root}", *args], text=True, capture_output=True, check=check)

    def _vars(self, redis_enabled: bool) -> list[str]:
        prefix = f"honua{self.name.replace('-', '')[:6]}{self.run_id[:6]}".lower()[:18]
        # The honua-iac modules validate admin_password length (>=32 chars) + complexity, so the
        # generated ephemeral test password must satisfy that contract or `terraform apply` fails at
        # variable validation before anything is provisioned.
        run = self.run_id or "local"
        admin_pw = os.environ.get("HONUA_ADMIN_PASSWORD") or f"HonuaCertIt-{run}-Aa1!-{run}-padpad"
        return [
            "-input=false", "-no-color",
            f"-var=region={self.region}",
            f"-var=name_prefix={prefix}",
            "-var=environment=it",
            f"-var={self.spec.image_var}={os.environ[self.spec.image_env]}",
            f"-var=honua_admin_password={admin_pw}",
            f"-var={self.spec.redis_var}={'true' if redis_enabled else 'false'}",
        ]

    def provision(self, redis_enabled: bool = False) -> str:
        root = self._iac_root()
        if root is None:
            raise ProvisionError(f"{self.name}: honua-iac root not found (set HONUA_IAC_DIR)")
        self._workdir = root
        self._last_vars = self._vars(redis_enabled)
        try:
            self._tf(root, "init", "-input=false", "-no-color")
            self._tf(root, "apply", "-auto-approve", *self._last_vars)
            out = self._tf(root, "output", "-raw", self.spec.endpoint_output)
        except subprocess.CalledProcessError as e:
            raise ProvisionError(f"{self.name} terraform failed: {e.stderr or e.stdout or e}") from e
        url = out.stdout.strip()
        if not url:
            raise ProvisionError(f"{self.name}: terraform applied but {self.spec.endpoint_output} was empty")
        return url

    def teardown(self) -> None:
        root = self._workdir or self._iac_root()
        if root is None:
            return
        try:
            self._tf(root, "destroy", "-auto-approve", *(self._last_vars or self._vars(False)), check=False)
        except Exception:  # noqa: BLE001 - best-effort reaper
            pass


# The two terraform-output cells. EKS is a separate, heavier target (cluster + Helm + LB).
SERVERLESS_SPEC = TfTargetSpec(
    name="aws-serverless",
    root="infrastructure/terraform/examples/aws-serverless",
    image_env="HONUA_LAMBDA_IMAGE_URI",
    image_var="honua_image_uri",
    image_hint="ECR Lambda-AOT image (*-lambda-aot)",
)
ECS_SPEC = TfTargetSpec(
    name="aws-ecs",
    root="infrastructure/terraform/examples/aws",
    image_env="HONUA_ECS_IMAGE",
    image_var="honua_image",
    image_hint="container image (ghcr or ECR; immutable tag/digest)",
)


def serverless(**kw) -> TerraformTarget:
    return TerraformTarget(SERVERLESS_SPEC, **kw)


def ecs(**kw) -> TerraformTarget:
    return TerraformTarget(ECS_SPEC, **kw)
