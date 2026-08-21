"""Config-driven Terraform deploy target — the shared driver for the AWS cells whose app endpoint is a
direct terraform output (serverless: Lambda+API GW; ECS: Fargate+ALB).

Each cell terraform-applies one honua-iac example root with the candidate image + the Redis toggle,
reads the `honua_url` output, and (the caller runs the canonical set) then destroys — ephemeral,
run-scoped, reaper-on-teardown. BLOCKED (honest, not green) until terraform + AWS creds + a deployable
image + the honua-iac tree are all present.
"""
from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
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
    # Extra terraform vars this root needs for an EPHEMERAL, reaped-on-teardown cert run. The ECS root's
    # ALB defaults enable_deletion_protection=true, which strands the ALB on `terraform destroy` (an
    # orphaned ALB every run) for the ecs cell. Force it off here (the shell integration harness already
    # does the same via TF_VAR_alb_deletion_protection=false). Serverless has no ALB, so it stays empty
    # there — the serverless root doesn't declare the var, and passing it would be a terraform error.
    ephemeral_vars: tuple[str, ...] = ()
    # JSON var files preserve typed values that cannot be represented faithfully
    # by Terraform's string-constrained `-var=name=value` coercion (notably null).
    # Paths are relative to the honua-release repository root.
    ephemeral_var_files: tuple[str, ...] = ()
    needs_runner_db_access: bool = False
    # honua-release#128 — the ECS cell's ALB security group.
    #
    # The aws-ecs module's ALB is internet-facing (`internal = false`, public subnets) so its DNS name
    # resolves from anywhere, but its SECURITY GROUP is not: with allow_http_ingress_cidrs and
    # allow_https_ingress_cidrs both unset and no certificate configured, the module falls back to
    #     http_ingress_cidrs = [vpc_cidr_block]
    # (modules/aws-ecs/main.tf locals; its README says so out loud: "the ALB listener defaults to
    # VPC-only ingress using the active VPC CIDR"). The GitHub-hosted runner is not in that VPC, so
    # every SYN to the ALB was dropped and every probe timed out — which is the whole of #128.
    #
    # The runner's own /32 is the correct opening: it is the same ephemeral address the PostGIS
    # bootstrap already opens RDS to, and the same one the EKS cell publishes its API server and load
    # balancer to. It admits exactly the caller doing the certifying and nothing else, so the cell gets
    # a reachable endpoint without ever putting a plain-HTTP ALB on 0.0.0.0/0 (which the module's own
    # `http_ingress_requires_https` check exists to discourage).
    needs_runner_alb_access: bool = False
    architecture_env: str = ""
    architecture_var: str = ""
    architecture_is_list: bool = False


class TerraformTarget(DeployTarget):
    supports_redis = True

    def __init__(self, spec: TfTargetSpec, *, run_id: str = "local", region: str | None = None) -> None:
        self.spec = spec
        self.name = spec.name
        self.run_id = run_id
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._workdir: Path | None = None
        self._last_vars: list[str] = []
        self._plan_path: Path | None = None
        self._provision_evidence: dict[str, str] = {}
        self._teardown_evidence: dict[str, str] = {}

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
        if self.spec.needs_runner_db_access and not os.environ.get("HONUA_AWS_DB_INGRESS_CIDR"):
            missing.append("HONUA_AWS_DB_INGRESS_CIDR (ephemeral runner /32 for PostGIS bootstrap)")
        if self.spec.needs_runner_alb_access and not os.environ.get("HONUA_AWS_RUNNER_CIDR"):
            missing.append("HONUA_AWS_RUNNER_CIDR (ephemeral runner /32 for ALB ingress)")
        if self.spec.architecture_env and not os.environ.get(self.spec.architecture_env):
            missing.append(f"{self.spec.architecture_env} (manifest-pinned runtime architecture)")
        if missing:
            return Availability(False, f"{self.name} not runnable: " + "; ".join(missing), missing)
        return Availability(True, f"{self.name} prerequisites present")

    # --- terraform lifecycle -------------------------------------------------------------------
    def _tf(self, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["terraform", f"-chdir={root}", *args], text=True, capture_output=True, check=check)

    def _vars(self, redis_enabled: bool) -> list[str]:
        # The Redis mode MUST be part of the prefix. The cert harness runs the redis-on and redis-off
        # cells for the same target with the SAME run_id (one GITHUB_RUN_ID across the whole matrix) IN
        # PARALLEL against one AWS account; an identical name_prefix collides on named resources (RDS
        # identifier, Lambda name, ...) and fails the redis-on cell spuriously (DBInstanceAlreadyExists
        # / ResourceExistsException). The `r`/`n` (redis / no-redis) tag, placed right after the "honua"
        # prefix so it survives the length cap, keeps the two cells' resource sets independent. Bounded
        # to 18 chars to stay well inside RDS(63)/Lambda(64) identifier budgets once the module suffixes.
        redis_tag = "r" if redis_enabled else "n"
        prefix = f"honua{redis_tag}{self.name.replace('-', '')[:5]}{self.run_id[:6]}".lower()[:18]
        # honua-iac requires at least 32 characters plus mixed-case, digit and special
        # characters. Keep the ephemeral fallback deterministic so the same value is
        # available to destroy after a partial apply.
        admin_pw = os.environ.get(
            "HONUA_ADMIN_PASSWORD",
            f"Honua-Gate-Aa1!CloudParity-00000000-{self.run_id}",
        )
        values = [
            "-input=false", "-no-color",
            *(f"-var-file={self._resolve_var_file(v)}" for v in self.spec.ephemeral_var_files),
            f"-var=region={self.region}",
            f"-var=name_prefix={prefix}",
            "-var=environment=it",
            f"-var={self.spec.image_var}={os.environ[self.spec.image_env]}",
            f"-var=honua_admin_password={admin_pw}",
            f"-var={self.spec.redis_var}={'true' if redis_enabled else 'false'}",
            *(f"-var={v}" for v in self.spec.ephemeral_vars),
        ]
        if self.spec.needs_runner_db_access:
            raw_cidr = self._runner_cidr("HONUA_AWS_DB_INGRESS_CIDR")
            values.extend([
                "-var=db_publicly_accessible=true",
                f"-var=db_additional_ingress_cidrs={json.dumps([raw_cidr], separators=(',', ':'))}",
            ])
        if self.spec.needs_runner_alb_access:
            raw_cidr = self._runner_cidr("HONUA_AWS_RUNNER_CIDR")
            values.append(
                f"-var=allow_http_ingress_cidrs={json.dumps([raw_cidr], separators=(',', ':'))}")
        if self.spec.architecture_env:
            architecture = os.environ.get(self.spec.architecture_env, "").strip()
            if architecture not in {"arm64", "x86_64"}:
                raise ProvisionError(
                    f"{self.name}: {self.spec.architecture_env} must be arm64 or x86_64, got {architecture!r}"
                )
            if not self.spec.architecture_var:
                raise ProvisionError(f"{self.name}: architecture_env requires architecture_var")
            architecture_value = (
                json.dumps([architecture], separators=(",", ":"))
                if self.spec.architecture_is_list
                else architecture.upper()
            )
            values.append(f"-var={self.spec.architecture_var}={architecture_value}")
        return values

    def _runner_cidr(self, env_name: str) -> str:
        """The ephemeral runner's own address, validated as a single IPv4 /32.

        A /32 is the point: these vars punch a hole in a security group, and the only caller that has
        any business coming through it is the one runner doing the certifying. Anything wider is
        rejected rather than quietly applied.
        """
        raw_cidr = os.environ.get(env_name, "").strip()
        try:
            cidr = ipaddress.ip_network(raw_cidr, strict=True)
        except ValueError as exc:
            raise ProvisionError(
                f"{self.name}: {env_name} must be a valid runner CIDR, got {raw_cidr!r}"
            ) from exc
        if cidr.version != 4 or cidr.prefixlen != 32:
            raise ProvisionError(
                f"{self.name}: {env_name} must be a single IPv4 /32, got {raw_cidr!r}"
            )
        return raw_cidr

    @staticmethod
    def _resolve_var_file(relative_path: str) -> Path:
        repo_root = Path(__file__).resolve().parents[2]
        path = (repo_root / relative_path).resolve()
        if not path.is_file():
            raise ProvisionError(f"ephemeral Terraform var file not found: {path}")
        return path

    def provision(self, redis_enabled: bool = False) -> str:
        root = self._iac_root()
        if root is None:
            raise ProvisionError(f"{self.name}: honua-iac root not found (set HONUA_IAC_DIR)")
        self._workdir = root
        self._last_vars = self._vars(redis_enabled)
        try:
            self._tf(root, "init", "-input=false", "-no-color")
            plan_dir = Path(tempfile.mkdtemp(prefix=f"honua-release-{self.name}-plan-"))
            self._plan_path = plan_dir / "candidate.tfplan"
            self._tf(root, "plan", f"-out={self._plan_path}", *self._last_vars)
            plan_sha256 = hashlib.sha256(self._plan_path.read_bytes()).hexdigest()
            self._tf(root, "apply", "-auto-approve", str(self._plan_path))
            self._provision_evidence = {
                "terraformPlanSha256": plan_sha256,
                "terraformApply": "passed",
            }
            out = self._tf(root, "output", "-raw", self.spec.endpoint_output)
        except (OSError, subprocess.CalledProcessError) as e:
            detail = getattr(e, "stderr", None) or getattr(e, "stdout", None) or str(e)
            raise ProvisionError(f"{self.name} terraform failed: {detail}") from e
        url = out.stdout.strip()
        if not url:
            raise ProvisionError(f"{self.name}: terraform applied but {self.spec.endpoint_output} was empty")
        return url

    @property
    def provision_evidence(self) -> dict[str, str]:
        """Secret-free proof that apply consumed the exact saved Terraform plan."""
        return dict(self._provision_evidence)

    def output_json(self, name: str):
        """Read one Terraform output without serializing it into the cloud gate report."""
        root = self._workdir or self._iac_root()
        if root is None:
            raise ProvisionError(f"{self.name}: honua-iac root not found (set HONUA_IAC_DIR)")
        try:
            value = self._tf(root, "output", "-json", name)
            return json.loads(value.stdout)
        except (json.JSONDecodeError, subprocess.CalledProcessError) as error:
            raise ProvisionError(f"{self.name}: could not read Terraform output {name}") from error

    @property
    def teardown_evidence(self) -> dict[str, str]:
        return dict(self._teardown_evidence)

    def teardown(self, redis_enabled: bool | None = None) -> None:
        # Fail-closed: a destroy that does not complete has left real, billing AWS resources behind
        # (honua-iac#142), so it raises instead of being swallowed. The caller (run_cloud / the
        # backstop reaper) turns that into a red cell, which is the only honest verdict for a cell
        # that stranded its own infrastructure.
        root = self._workdir or self._iac_root()
        if root is None:
            return
        mode = False if redis_enabled is None else redis_enabled
        cleanup_error: OSError | None = None
        try:
            destroy = self._tf(root, "destroy", "-auto-approve",
                               *(self._last_vars or self._vars(mode)), check=False)
        except OSError as error:
            raise ProvisionError(f"{self.name} teardown failed: {error}") from error
        finally:
            try:
                if self._plan_path is not None:
                    self._plan_path.unlink(missing_ok=True)
                    self._plan_path.parent.rmdir()
                    self._plan_path = None
            except OSError as error:
                cleanup_error = error
        if destroy.returncode != 0:
            detail = (destroy.stderr or destroy.stdout or "terraform destroy returned nonzero").strip()
            suffix = "; saved plan cleanup also failed" if cleanup_error else ""
            raise ProvisionError(f"{self.name} teardown failed: {detail}{suffix}")
        state = self._tf(root, "state", "list", check=False)
        if state.returncode != 0 or state.stdout.strip():
            raise ProvisionError(
                f"{self.name} teardown failed: Terraform state is unavailable or still owns resources"
            )
        self._teardown_evidence = {
            "terraformDestroy": "passed",
            "cleanupVerified": "passed",
        }
        if cleanup_error is not None:
            raise ProvisionError(f"{self.name} teardown failed: could not remove the saved Terraform plan")


# The two terraform-output cells. EKS is a separate, heavier target (cluster + Helm + LB).
SERVERLESS_SPEC = TfTargetSpec(
    name="aws-serverless",
    root="infrastructure/terraform/examples/aws-serverless",
    image_env="HONUA_LAMBDA_IMAGE_URI",
    image_var="honua_image_uri",
    image_hint="ECR Lambda-AOT image (*-lambda-aot)",
    needs_runner_db_access=True,
    architecture_env="HONUA_LAMBDA_ARCHITECTURE",
    architecture_var="lambda_architectures",
    architecture_is_list=True,
)
ECS_SPEC = TfTargetSpec(
    name="aws-ecs",
    root="infrastructure/terraform/examples/aws",
    image_env="HONUA_ECS_IMAGE",
    image_var="honua_image",
    image_hint="container image (ghcr or ECR; immutable tag/digest)",
    # This is always a brand-new ephemeral deployment, so explicitly select the
    # IAC root's null/new-key path. Existing deployments must supply their current
    # key instead, but the release harness never adopts an existing ECS database.
    # The manifest explicitly selects the proven architecture and excludes the broken ARM64 child.
    ephemeral_vars=("alb_deletion_protection=false",),
    ephemeral_var_files=("e2e/terraform/aws-ecs-new-deployment.tfvars.json",),
    needs_runner_db_access=True,
    needs_runner_alb_access=True,
    architecture_env="HONUA_ECS_ARCHITECTURE",
    architecture_var="task_cpu_architecture",
)


def serverless(**kw) -> TerraformTarget:
    return TerraformTarget(SERVERLESS_SPEC, **kw)


def ecs(**kw) -> TerraformTarget:
    return TerraformTarget(ECS_SPEC, **kw)
