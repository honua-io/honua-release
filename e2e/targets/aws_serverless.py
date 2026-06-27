"""AWS serverless deploy target (Lambda + API Gateway) — the first cloud parity cell.

Drives the REAL honua-iac serverless example
(`infrastructure/terraform/examples/aws-serverless`, module `modules/aws-serverless`): terraform
apply with the candidate image -> read the `honua_url` output -> (caller runs the canonical set) ->
terraform destroy. Built per docs/TEST-STRATEGY.md Phase B (AWS-first, scale-to-zero, OIDC, ephemeral
+ teardown reaper).

It is BLOCKED (honest, not green) until ALL of these are wired — each is a real prerequisite, not a
stub we can fake:
  - terraform CLI on PATH
  - AWS credentials (OIDC role assumed by the workflow, or AWS_* env)
  - a deployable Lambda image: the serverless module wants an ECR `honua_image_uri` (`*-lambda-aot`),
    NOT the ghcr image the manifest pins — so a published Lambda-AOT image in ECR is required
    (env HONUA_LAMBDA_IMAGE_URI)
  - the honua-iac terraform tree (env HONUA_IAC_DIR, or a checkout)

Resource hygiene: every apply is tagged ephemeral + carries the run id, time-boxed, and `teardown()`
always runs (the reaper) so nothing lingers on credits.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .base import Availability, DeployTarget, ProvisionError

# Relative path of the serverless root inside the honua-iac tree.
SERVERLESS_ROOT = "infrastructure/terraform/examples/aws-serverless"


class AwsServerlessTarget(DeployTarget):
    name = "aws-serverless"

    def __init__(self, *, run_id: str = "local", region: str | None = None) -> None:
        self.run_id = run_id
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._workdir: Path | None = None

    # --- prerequisites -------------------------------------------------------------------------
    def _iac_root(self) -> Path | None:
        base = os.environ.get("HONUA_IAC_DIR")
        if base:
            root = Path(base) / SERVERLESS_ROOT
            return root if root.is_dir() else None
        return None

    @staticmethod
    def _has_aws_creds() -> bool:
        # OIDC sets AWS_* in the job; a self-hosted/profile run may use a profile.
        return bool(
            os.environ.get("AWS_ACCESS_KEY_ID")
            or os.environ.get("AWS_ROLE_ARN")
            or os.environ.get("AWS_PROFILE")
            or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
        )

    def availability(self) -> Availability:
        missing: list[str] = []
        if not shutil.which("terraform"):
            missing.append("terraform CLI")
        if not self._has_aws_creds():
            missing.append("AWS credentials (OIDC role / AWS_* env)")
        if not os.environ.get("HONUA_LAMBDA_IMAGE_URI"):
            missing.append("HONUA_LAMBDA_IMAGE_URI (ECR Lambda-AOT image)")
        if self._iac_root() is None:
            missing.append("HONUA_IAC_DIR pointing at the honua-iac terraform tree")
        if missing:
            return Availability(False, "aws-serverless not runnable: " + "; ".join(missing), missing)
        return Availability(True, "aws-serverless prerequisites present")

    # --- terraform lifecycle -------------------------------------------------------------------
    def _tf(self, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["terraform", f"-chdir={root}", *args],
            text=True, capture_output=True, check=check,
        )

    def _common_vars(self) -> list[str]:
        # Ephemeral, run-scoped naming so concurrent runs never collide and the reaper can find them.
        prefix = f"honuait{self.run_id[:8]}".lower().replace("_", "").replace("-", "")[:16]
        admin_pw = os.environ.get("HONUA_ADMIN_PASSWORD", f"it-{self.run_id}-Aa1!")
        image = os.environ["HONUA_LAMBDA_IMAGE_URI"]
        return [
            "-input=false", "-no-color",
            f"-var=region={self.region}",
            f"-var=name_prefix={prefix}",
            "-var=environment=it",
            f"-var=honua_image_uri={image}",
            f"-var=honua_admin_password={admin_pw}",
        ]

    def provision(self) -> str:
        root = self._iac_root()
        if root is None:
            raise ProvisionError("honua-iac serverless root not found (set HONUA_IAC_DIR)")
        self._workdir = root
        try:
            self._tf(root, "init", "-input=false", "-no-color")
            self._tf(root, "apply", "-auto-approve", *self._common_vars())
            out = self._tf(root, "output", "-raw", "honua_url")
        except subprocess.CalledProcessError as e:
            raise ProvisionError(f"terraform failed: {e.stderr or e.stdout or e}") from e
        url = out.stdout.strip()
        if not url:
            raise ProvisionError("terraform applied but honua_url output was empty")
        return url

    def teardown(self) -> None:
        # The reaper: always safe to call. Never raise — a failed destroy must not mask the run result
        # (a separate scheduled reaper sweeps anything this misses).
        root = self._workdir or self._iac_root()
        if root is None:
            return
        try:
            self._tf(root, "destroy", "-auto-approve", *self._common_vars(), check=False)
        except Exception:  # noqa: BLE001 - best-effort
            pass
