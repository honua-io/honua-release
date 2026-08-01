"""AWS EKS deploy target — the heaviest cell (k8s control plane + Helm + LoadBalancer).

Unlike serverless/ECS, EKS does not expose the app URL as a terraform output: terraform stands up the
cluster, the honua-helm chart is installed onto it (with Redis toggled via a chart value), and the app
endpoint is the provisioned LoadBalancer hostname. So this target needs the full k8s toolchain
(kubectl + helm + the chart) on top of terraform/AWS/image, and is correspondingly run least often
(TEST-STRATEGY: "EKS weekly, not nightly; high + slow").

It is BLOCKED (honest) until that whole toolchain is wired; the cluster-apply step is implemented, and
the Helm-install + LoadBalancer-resolve frontier is clearly marked so it fails closed rather than
fabricating an endpoint.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .base import Availability, DeployTarget, ProvisionError

EKS_ROOT = "infrastructure/terraform/examples/aws-eks"


class AwsEksTarget(DeployTarget):
    name = "aws-eks"
    supports_redis = True

    def __init__(self, *, run_id: str = "local", region: str | None = None) -> None:
        self.run_id = run_id
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._workdir: Path | None = None
        self._prefix: str | None = None

    def _name_prefix(self, redis_enabled: bool) -> str:
        # Redis mode in the prefix so the redis-on and redis-off EKS cells (same run_id, run in parallel)
        # provision independent, non-colliding cluster resource names. Bounded to 18 chars. Stored on
        # provision so teardown reaps the exact same names it applied.
        redis_tag = "r" if redis_enabled else "n"
        return f"honuaeks{redis_tag}{self.run_id[:6]}".lower()[:18]

    def _iac_root(self) -> Path | None:
        base = os.environ.get("HONUA_IAC_DIR")
        if not base:
            return None
        root = Path(base) / EKS_ROOT
        return root if root.is_dir() else None

    @staticmethod
    def _has_aws_creds() -> bool:
        return bool(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ROLE_ARN")
                    or os.environ.get("AWS_PROFILE") or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"))

    def availability(self) -> Availability:
        missing: list[str] = []
        for tool in ("terraform", "kubectl", "helm"):
            if not shutil.which(tool):
                missing.append(f"{tool} CLI")
        if not self._has_aws_creds():
            missing.append("AWS credentials (OIDC role / AWS_* env)")
        if not os.environ.get("HONUA_ECS_IMAGE"):
            missing.append("HONUA_ECS_IMAGE (container image for the k8s deployment)")
        if self._iac_root() is None:
            missing.append("HONUA_IAC_DIR pointing at the honua-iac terraform tree")
        if not os.environ.get("HONUA_HELM_DIR"):
            missing.append("HONUA_HELM_DIR pointing at the honua-helm chart")
        if missing:
            return Availability(False, f"{self.name} not runnable: " + "; ".join(missing), missing)
        return Availability(True, f"{self.name} prerequisites present")

    def _tf(self, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["terraform", f"-chdir={root}", *args], text=True, capture_output=True, check=check)

    def provision(self, redis_enabled: bool = False) -> str:
        root = self._iac_root()
        if root is None:
            raise ProvisionError(f"{self.name}: honua-iac EKS root not found (set HONUA_IAC_DIR)")
        self._workdir = root
        prefix = self._prefix = self._name_prefix(redis_enabled)
        try:
            self._tf(root, "init", "-input=false", "-no-color")
            self._tf(root, "apply", "-auto-approve", "-input=false", "-no-color",
                     f"-var=region={self.region}", f"-var=name_prefix={prefix}", "-var=environment=it")
        except subprocess.CalledProcessError as e:
            raise ProvisionError(f"{self.name} cluster terraform failed: {e.stderr or e.stdout or e}") from e

        # Frontier: helm install honua-helm (redis_enabled -> chart value) onto the cluster, then wait
        # for the Service LoadBalancer hostname. Not yet wired — fail closed rather than fake a URL.
        raise ProvisionError(
            f"{self.name}: cluster applied, but helm-install + LoadBalancer-resolve is not wired yet "
            f"(redis_enabled={redis_enabled}); set up `helm install honua $HONUA_HELM_DIR "
            f"--set redis.enabled={str(redis_enabled).lower()}` + kubectl wait for the LB hostname"
        )

    def teardown(self, redis_enabled: bool | None = None) -> None:
        root = self._workdir or self._iac_root()
        if root is None:
            return
        try:
            mode = False if redis_enabled is None else redis_enabled
            prefix = self._prefix or self._name_prefix(mode)
            self._tf(root, "destroy", "-auto-approve", "-input=false", "-no-color",
                     f"-var=region={self.region}", f"-var=name_prefix={prefix}", "-var=environment=it", check=False)
        except Exception:  # noqa: BLE001 - best-effort reaper
            pass
