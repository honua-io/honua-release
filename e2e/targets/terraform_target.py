"""Config-driven Terraform deploy target — the shared driver for the AWS cells whose app endpoint is a
direct terraform output (serverless: Lambda+API GW; ECS: Fargate+ALB).

Each cell terraform-applies one honua-iac example root with the candidate image + the Redis toggle,
reads the `honua_url` output, and (the caller runs the canonical set) then destroys — ephemeral,
run-scoped, reaper-on-teardown. BLOCKED (honest, not green) until terraform + AWS creds + a deployable
image + the honua-iac tree are all present.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
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
    # `name=value` vars passed ONLY when the pinned root declares `name` (Terraform rejects an
    # undeclared -var), so one spec works across iac pins that added the input later. honua-iac
    # v0.2.0's examples/aws flipped two defaults that an ephemeral cell cannot live with:
    #   * rds_deletion_protection defaults true, so `terraform destroy` cannot delete the RDS
    #     instance and strands it with the VPC (e2e-cloud-aws run 36560629698);
    #   * enable_postgis defaults false (private-RDS production shape), so the server's PostGIS
    #     preflight exits at startup and the ALB answers 503. The cell's RDS is publicly accessible
    #     to the runner's /32 (needs_runner_db_access), which is exactly the reachability the
    #     local-exec PostGIS bootstrap requires, as on v0.1.0 where it defaulted true.
    declared_ephemeral_vars: tuple[str, ...] = ()
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
    # (ENV_NAME, terraform_var) pairs. The env value is passed as `-var=terraform_var=<value>` when
    # the pinned root declares terraform_var; a declaring root with the env unset is BLOCKED rather
    # than applied with the module's fallback (the Lambda+Batch cell must run the pinned Batch image).
    env_vars: tuple[tuple[str, str], ...] = ()
    # (ENV_NAME, terraform_var) pairs for redis-on cells only, passed when the env is set and the
    # pinned root declares terraform_var. A var named in redis_required_vars that the root declares
    # but whose env is unset refuses provisioning with the owner step, rather than letting the plan
    # refuse later with a module error; destroy never refuses (a cell that could not plan built nothing).
    redis_env_vars: tuple[tuple[str, str], ...] = ()
    redis_required_vars: tuple[str, ...] = ()
    # (ENV_NAME, terraform_var) pairs for recommended inputs: passed when the env is set and the
    # pinned root declares terraform_var, on every cell. Unlike env_vars, an unset env never blocks or
    # refuses; the root plans with its own default (and its own warning).
    optional_env_vars: tuple[tuple[str, str], ...] = ()
    # Opt-in vars: passed only when `opt_in_env` is "true". A root that does not declare one of them
    # is a provisioning failure, not a silent drop: the caller asked for that capability.
    opt_in_env: str = ""
    opt_in_vars: tuple[str, ...] = ()
    # Run the generic server image on the runner against the cell's database before the cell is
    # probed. The serverless root keeps skip_migrations=true (a Lambda cold start must not race a
    # schema migration), so without this step the Lambda serves an unmigrated database.
    migrate_image_env: str = ""
    # honua-release#450: give the cell a per-run HTTPS hostname under the account's public hosted
    # zone (domain_name + route53_zone_id: the module issues an ACM certificate, validates it in that
    # zone and aliases the name to the ALB), so the pinned honua CLI/MCP proxy accept the endpoint
    # (HTTPS-only for credentials) and the reviewed demo CSP admits it (https://*.honua.io). Only when
    # both CELL_DNS_ZONE_ENV and CELL_DNS_PARENT_ENV are set; otherwise the cell keeps its plain-HTTP
    # ALB endpoint, unchanged.
    cell_domain: bool = False
    # Browser origins the cell must answer CORS for, passed as cors_allowed_origins when the pinned
    # root declares it (honua-iac fix unit C4 renders them as Cors__AllowedOrigins__<n>).
    cors_allowed_origins: tuple[str, ...] = ()


# Repository variables (not secrets) naming the public hosted zone that carries per-run cell names.
CELL_DNS_ZONE_ENV = "HONUA_AWS_CELL_DNS_ZONE_ID"
CELL_DNS_PARENT_ENV = "HONUA_AWS_CELL_DNS_PARENT"
# Cell names live one level below the parent (`<label>.cert.<parent>`), apart from anything else the
# zone serves, so the teardown check can tell the harness's records from the rest of the zone.
CELL_DNS_SUBDOMAIN = "cert"
# The demo pages are served from this loopback origin by e2e/drivers/demos/run.sh (E2E_SITE_PORT).
DEMO_SITE_ORIGIN = "http://127.0.0.1:18099"
# ACM limits the certificate's first domain name to 64 octets; one DNS label is at most 63.
_ACM_DOMAIN_MAX = 64
_DNS_LABEL_MAX = 63
_ZONE_ID = re.compile(r"^Z[A-Z0-9]{1,31}$")
_DNS_NAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class TerraformTarget(DeployTarget):
    supports_redis = True

    def __init__(self, spec: TfTargetSpec, *, run_id: str = "local", region: str | None = None) -> None:
        self.spec = spec
        self.name = spec.name
        self.admission = "alb" if spec.needs_runner_alb_access else "none"
        self.run_id = run_id
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._workdir: Path | None = None
        self._last_vars: list[str] = []

    @property
    def admin_api_key(self) -> str:
        # Never derived: the Actions run id is public, so a password computed from it is a published
        # credential for a public endpoint. The provision phase mints one per cell.
        value = os.environ.get("HONUA_ADMIN_PASSWORD", "")
        if not value:
            raise ProvisionError(f"{self.name}: HONUA_ADMIN_PASSWORD is unset; refusing to derive an "
                                 "admin password from the public run id")
        return value

    @property
    def migrates_before_serving(self) -> bool:
        return bool(self.spec.migrate_image_env)

    # --- prerequisites -------------------------------------------------------------------------
    def _iac_root(self) -> Path | None:
        base = os.environ.get("HONUA_IAC_DIR")
        if not base:
            return None
        root = Path(base) / self.spec.root
        return root if root.is_dir() else None

    def _root_declares(self, variable: str) -> bool:
        """True when the pinned honua-iac root declares `variable`. Passing an undeclared -var is a
        hard terraform error, and the example roots set module-only inputs (such as additional_env)
        internally rather than exposing them."""
        root = self._iac_root()
        if root is None:
            return False
        # Anchored like AwsEksTarget._root_declares: a comment that merely mentions the variable
        # must not count as a declaration.
        pattern = re.compile(r'^\s*variable\s+"' + re.escape(variable) + r'"\s*\{', re.MULTILINE)
        for tf_file in sorted(root.glob("*.tf")):
            try:
                body = tf_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if pattern.search(body):
                return True
        return False

    def _licensing_vars(self) -> list[str]:
        # 2026.1 ships licensing disabled (honua-release#338). Roots that expose licensing_mode get it
        # explicitly; older roots with no licensing input get nothing. Either way the cell's runtime
        # licensing assertion (GET /api/v1/admin/license reports disabled) decides pass or fail.
        return ["-var=licensing_mode=Disabled"] if self._root_declares("licensing_mode") else []

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
        for env_name, variable in self.spec.env_vars:
            if not os.environ.get(env_name) and self._root_declares(variable):
                missing.append(f"{env_name} (pinned value for terraform var {variable})")
        if self.spec.migrate_image_env and not os.environ.get(self.spec.migrate_image_env):
            missing.append(f"{self.spec.migrate_image_env} (generic server image for the pre-serving migration)")
        if self.spec.migrate_image_env and not shutil.which("docker"):
            missing.append("docker CLI (pre-serving migration)")
        if missing:
            return Availability(False, f"{self.name} not runnable: " + "; ".join(missing), missing)
        return Availability(True, f"{self.name} prerequisites present")

    # --- terraform lifecycle -------------------------------------------------------------------
    def _tf(self, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["terraform", f"-chdir={root}", *args], text=True, capture_output=True, check=check)

    def _vars(self, redis_enabled: bool, *, destroy: bool = False) -> list[str]:
        # The Redis mode MUST be part of the prefix. The cert harness runs the redis-on and redis-off
        # cells for the same target with the SAME run_id (one GITHUB_RUN_ID across the whole matrix) IN
        # PARALLEL against one AWS account; an identical name_prefix collides on named resources (RDS
        # identifier, Lambda name, ...) and fails the redis-on cell spuriously (DBInstanceAlreadyExists
        # / ResourceExistsException). The `r`/`n` (redis / no-redis) tag, placed right after the "honua"
        # prefix so it survives the length cap, keeps the two cells' resource sets independent. Bounded
        # to 18 chars to stay well inside RDS(63)/Lambda(64) identifier budgets once the module suffixes.
        redis_tag = "r" if redis_enabled else "n"
        prefix = f"honua{redis_tag}{self.name.replace('-', '')[:5]}{self.run_id[:6]}".lower()[:18]
        # honua-iac requires at least 32 characters plus mixed-case, digit and special characters.
        # The teardown job is a fresh runner without the provision job's password; destroy evaluates
        # but never applies the value, so a throwaway one satisfies the input contract there.
        if destroy and not os.environ.get("HONUA_ADMIN_PASSWORD"):
            admin_pw = f"Honua-Destroy-Aa1!{secrets.token_urlsafe(32)}"
        else:
            admin_pw = self.admin_api_key
        values = [
            "-input=false", "-no-color",
            *(f"-var-file={self._resolve_var_file(v)}" for v in self.spec.ephemeral_var_files),
            f"-var=region={self.region}",
            f"-var=name_prefix={prefix}",
            "-var=environment=it",
            f"-var={self.spec.image_var}={os.environ[self.spec.image_env]}",
            f"-var=honua_admin_password={admin_pw}",
            *self._licensing_vars(),
            f"-var={self.spec.redis_var}={'true' if redis_enabled else 'false'}",
            *(f"-var={v}" for v in self.spec.ephemeral_vars),
            *(f"-var={v}" for v in self.spec.declared_ephemeral_vars
              if self._root_declares(v.split("=", 1)[0])),
            *(f"-var={variable}={os.environ[env_name]}" for env_name, variable in self.spec.env_vars
              if os.environ.get(env_name) and self._root_declares(variable)),
            *(f"-var={variable}={os.environ[env_name].strip()}" for env_name, variable in self.spec.optional_env_vars
              if os.environ.get(env_name, "").strip() and self._root_declares(variable)),
        ]
        if redis_enabled:
            declared = [(env_name, variable) for env_name, variable in self.spec.redis_env_vars
                        if self._root_declares(variable)]
            values.extend(f"-var={variable}={os.environ[env_name].strip()}" for env_name, variable in declared
                          if os.environ.get(env_name, "").strip())
            unset = [env_name for env_name, variable in declared
                     if variable in self.spec.redis_required_vars and not os.environ.get(env_name, "").strip()]
            if unset and not destroy:
                raise ProvisionError(
                    f"{self.name}: the pinned root requires {', '.join(unset)} on a Redis-on cell (an "
                    "operator-owned Secrets Manager secret holding the operation key-ring certificate; "
                    "owner step in e2e/README.md)")
        if self.spec.opt_in_env and os.environ.get(self.spec.opt_in_env, "").strip().lower() == "true":
            undeclared = [v.split("=", 1)[0] for v in self.spec.opt_in_vars
                          if not self._root_declares(v.split("=", 1)[0])]
            if undeclared:
                raise ProvisionError(f"{self.name}: {self.spec.opt_in_env}=true but the pinned root does "
                                     f"not declare {', '.join(undeclared)}")
            values.extend(f"-var={v}" for v in self.spec.opt_in_vars)
        if self.spec.needs_runner_db_access:
            raw_cidr = self._runner_cidr("HONUA_AWS_DB_INGRESS_CIDR")
            values.extend([
                "-var=db_publicly_accessible=true",
                f"-var=db_additional_ingress_cidrs={json.dumps([raw_cidr], separators=(',', ':'))}",
            ])
        domain = self._domain_vars(redis_enabled, destroy=destroy)
        values.extend(domain)
        if self.spec.needs_runner_alb_access:
            raw_cidr = self._runner_cidr("HONUA_AWS_RUNNER_CIDR")
            # With a certificate the runner is admitted to the HTTPS listener; plain-HTTP ingress is
            # not requested. (The module still serves a redirect-only listener on 80 to the same /32,
            # because examples/aws does not expose alb_enable_http_redirect.)
            ingress = "allow_https_ingress_cidrs" if domain else "allow_http_ingress_cidrs"
            values.append(f"-var={ingress}={json.dumps([raw_cidr], separators=(',', ':'))}")
        if self.spec.cors_allowed_origins and self._root_declares("cors_allowed_origins"):
            values.append("-var=cors_allowed_origins="
                          + json.dumps(list(self.spec.cors_allowed_origins), separators=(",", ":")))
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

    # --- per-run HTTPS cell hostname (honua-release#450) -----------------------------------------
    def cell_domain_label(self, redis_enabled: bool) -> str:
        """The DNS label naming this cell in this run: `<run id>-<target>-redis-<on|off>`.

        Unique per run (the full Actions run id, never truncated) and per cell (target and Redis
        mode), so concurrent cells and concurrent runs never share a certificate or alias record.
        Deterministic, so the teardown job (a fresh runner) recomputes the name the apply used.
        Lowercase LDH, at most 63 octets and short enough that `<label>.cert.<parent>` fits ACM's
        64-octet limit; an over-long value keeps a hash suffix rather than being cut to a prefix
        another run could share.
        """
        raw = f"{self.run_id}-{self.name}-redis-{'on' if redis_enabled else 'off'}".lower()
        label = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "-", raw)).strip("-")
        parent = os.environ.get(CELL_DNS_PARENT_ENV, "").strip().lower().rstrip(".")
        budget = min(_DNS_LABEL_MAX, _ACM_DOMAIN_MAX - len(f".{CELL_DNS_SUBDOMAIN}.{parent}"))
        if budget < 16:
            raise ProvisionError(f"{self.name}: {CELL_DNS_PARENT_ENV}={parent!r} leaves no room for a "
                                 "cell label under ACM's 64-octet domain limit")
        if len(label) > budget:
            digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
            label = f"{label[:budget - 11].rstrip('-')}-{digest}"
        return label

    def cell_domain(self, redis_enabled: bool) -> tuple[str, str] | None:
        """(fqdn, hosted zone id) for this cell, or None when the repository variables are unset.

        Both variables or neither: one without the other is an owner misconfiguration and refuses
        rather than silently provisioning plain HTTP.
        """
        if not self.spec.cell_domain:
            return None
        zone = os.environ.get(CELL_DNS_ZONE_ENV, "").strip()
        parent = os.environ.get(CELL_DNS_PARENT_ENV, "").strip().lower().rstrip(".")
        if not zone and not parent:
            return None
        if not zone or not parent:
            missing = CELL_DNS_ZONE_ENV if not zone else CELL_DNS_PARENT_ENV
            raise ProvisionError(f"{self.name}: {missing} is unset while the other cell DNS variable is "
                                 "set; set both (owner step in e2e/README.md) or neither")
        if not _ZONE_ID.fullmatch(zone):
            raise ProvisionError(f"{self.name}: {CELL_DNS_ZONE_ENV} is not a Route53 hosted zone id")
        if not _DNS_NAME.fullmatch(parent):
            raise ProvisionError(f"{self.name}: {CELL_DNS_PARENT_ENV} is not a DNS name")
        return f"{self.cell_domain_label(redis_enabled)}.{CELL_DNS_SUBDOMAIN}.{parent}", zone

    def _domain_vars(self, redis_enabled: bool, *, destroy: bool) -> list[str]:
        try:
            domain = self.cell_domain(redis_enabled)
        except ProvisionError:
            if destroy:
                # Destroy removes what the state holds whatever the inputs; it never refuses.
                return []
            raise
        if domain is None:
            return []
        undeclared = [v for v in ("domain_name", "route53_zone_id", "allow_https_ingress_cidrs")
                      if not self._root_declares(v)]
        if undeclared:
            if destroy:
                return []
            raise ProvisionError(f"{self.name}: {CELL_DNS_ZONE_ENV} is set but the pinned root does not "
                                 f"declare {', '.join(undeclared)}")
        fqdn, zone = domain
        return [f"-var=domain_name={fqdn}", f"-var=route53_zone_id={zone}"]

    def _cell_dns_leftovers(self, redis_enabled: bool, *, run=subprocess.run) -> None:
        """After a successful destroy: this cell's certificate and records must be gone.

        Read-only. A record or certificate of THIS cell still present fails teardown closed (a
        stranded certificate or record is a leftover like any other). Other `*.cert.<parent>` names
        are listed as a warning only: they may belong to cells of a concurrent run.
        """
        try:
            domain = self.cell_domain(redis_enabled)
        except ProvisionError:
            return
        if domain is None:
            return
        fqdn, zone = domain
        parent = fqdn.split(".", 1)[1]  # cert.<parent>

        def aws(*args: str):
            return run(["aws", *args, "--output", "json"], text=True, capture_output=True, check=False)

        records = aws("route53", "list-resource-record-sets", "--hosted-zone-id", zone)
        certificates = aws("acm", "list-certificates", "--region", self.region)
        if records.returncode or certificates.returncode:
            print(f"::warning title=cell DNS leftovers::could not list the {parent} records or ACM "
                  "certificates to confirm this cell's names were destroyed", flush=True)
            return
        names = sorted({str(r.get("Name", "")).lower().rstrip(".")
                        for r in json.loads(records.stdout or "{}").get("ResourceRecordSets", [])})
        harness = [n for n in names if n.endswith("." + parent)]
        own = [n for n in harness if n == fqdn or n.endswith("." + fqdn)]
        own_certs = [c for c in json.loads(certificates.stdout or "{}").get("CertificateSummaryList", [])
                     if str(c.get("DomainName", "")).lower().rstrip(".") == fqdn]
        others = [n for n in harness if n not in own]
        if others:
            print(f"::warning title=cell DNS names present::{len(others)} other *.{parent} record(s) "
                  f"remain (live cells of a concurrent run, or strands): {', '.join(others[:20])}",
                  flush=True)
        if own or own_certs:
            raise ProvisionError(f"{self.name} teardown left {fqdn} behind: "
                                 f"{len(own)} Route53 record(s), {len(own_certs)} ACM certificate(s)")

    def _runner_cidr(self, env_name: str) -> str:
        """The ephemeral runner's own address, validated as a single IPv4 /32.

        A /32 is the point: these vars punch a hole in a security group, and the only caller that has
        any business coming through it is the one runner doing the certifying. Anything wider is
        rejected rather than quietly applied.
        """
        return self._host_cidr(os.environ.get(env_name, ""), env_name)

    def _host_cidr(self, raw_cidr: str, label: str) -> str:
        raw_cidr = raw_cidr.strip()
        try:
            cidr = ipaddress.ip_network(raw_cidr, strict=True)
        except ValueError as exc:
            raise ProvisionError(
                f"{self.name}: {label} must be a valid runner CIDR, got {raw_cidr!r}"
            ) from exc
        if cidr.version != 4 or cidr.prefixlen != 32:
            raise ProvisionError(
                f"{self.name}: {label} must be a single IPv4 /32, got {raw_cidr!r}"
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
        if self.spec.cell_domain:
            domain = self.cell_domain(redis_enabled)
            if domain is None:
                print(f"{self.name}: {CELL_DNS_ZONE_ENV}/{CELL_DNS_PARENT_ENV} unset; the cell keeps its "
                      "plain-HTTP load balancer endpoint (no per-run HTTPS hostname, honua-release#450)",
                      flush=True)
            else:
                print(f"{self.name}: HTTPS cell hostname {domain[0]} (ACM DNS validation in the hosted "
                      "zone; the apply waits for issuance)", flush=True)
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

    def teardown(self, redis_enabled: bool | None = None) -> None:
        # Fail-closed: a destroy that does not complete has left real, billing AWS resources behind
        # (honua-iac#142), so it raises instead of being swallowed. The caller (run_cloud / the
        # backstop reaper) turns that into a red cell, which is the only honest verdict for a cell
        # that stranded its own infrastructure.
        root = self._workdir or self._iac_root()
        if root is None:
            return
        mode = False if redis_enabled is None else redis_enabled
        try:
            destroy = self._tf(root, "destroy", "-auto-approve",
                               *(self._last_vars or self._vars(mode, destroy=True)), check=False)
        except OSError as error:
            raise ProvisionError(f"{self.name} teardown failed: {error}") from error
        if destroy.returncode != 0:
            detail = (destroy.stderr or destroy.stdout or "terraform destroy returned nonzero").strip()
            raise ProvisionError(f"{self.name} teardown failed: {detail}")
        self._cell_dns_leftovers(mode)

    def admit(self, endpoint: str, cidr: str, *, redis_enabled: bool = False, run=subprocess.run) -> None:
        """Add one runner /32 to the security groups of the load balancer that serves `endpoint`.

        The journey runs on a second, credential-free runner (honua-release#381); the ALB was opened
        only to the provisioning runner. The rule disappears with the security group at teardown.
        """
        if not self.spec.needs_runner_alb_access:
            return
        cidr = self._host_cidr(cidr, "journey runner CIDR")
        url = urllib.parse.urlsplit(endpoint)
        host = (url.hostname or "").lower()
        port = url.port or (443 if url.scheme == "https" else 80)

        def aws(*args: str) -> subprocess.CompletedProcess:
            return run(["aws", *args, "--region", self.region, "--output", "json"],
                       text=True, capture_output=True, check=False)

        listing = aws("elbv2", "describe-load-balancers")
        if listing.returncode:
            raise ProvisionError(f"{self.name}: could not list load balancers to admit the journey runner")
        balancers = json.loads(listing.stdout).get("LoadBalancers", [])
        if not any(str(b.get("DNSName", "")).lower() == host for b in balancers):
            # A per-run HTTPS hostname (honua-release#450) is a Route53 alias of the cell's ALB.
            host = self._alias_target(host, aws) or host
        groups = [group for balancer in balancers
                  if str(balancer.get("DNSName", "")).lower() == host
                  for group in balancer.get("SecurityGroups", [])]
        if not groups:
            raise ProvisionError(f"{self.name}: no load balancer serves {host!r}")
        permission = json.dumps([{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                                  "IpRanges": [{"CidrIp": cidr, "Description": "honua-release#381 journey runner"}]}])
        for group in groups:
            result = aws("ec2", "authorize-security-group-ingress", "--group-id", group,
                         "--ip-permissions", permission)
            if result.returncode and "InvalidPermission.Duplicate" not in (result.stderr or ""):
                raise ProvisionError(f"{self.name}: could not admit the journey runner to {group}")

    @staticmethod
    def _alias_target(host: str, aws) -> str | None:
        """The load balancer DNS name the cell hosted zone aliases `host` to, or None."""
        zone = os.environ.get(CELL_DNS_ZONE_ENV, "").strip()
        if not zone or not _ZONE_ID.fullmatch(zone):
            return None
        listing = aws("route53", "list-resource-record-sets", "--hosted-zone-id", zone,
                      "--start-record-name", host, "--start-record-type", "A", "--max-items", "1")
        if listing.returncode:
            return None
        for record in json.loads(listing.stdout or "{}").get("ResourceRecordSets", []):
            if (str(record.get("Name", "")).lower().rstrip(".") == host and record.get("Type") == "A"
                    and record.get("AliasTarget")):
                target = str(record["AliasTarget"].get("DNSName", "")).lower().rstrip(".")
                return target.removeprefix("dualstack.")
        return None

    def _state_secrets(self, names: tuple[str, ...]) -> list[str]:
        """The cell's Secrets Manager values named `names`, read from Terraform state in memory."""
        if self._workdir is None:
            raise ProvisionError("cloud database is not provisioned")
        state = json.loads(self._tf(self._workdir, "show", "-json").stdout)

        def walk(module):
            for resource in module.get("resources", []):
                if resource.get("type") == "aws_secretsmanager_secret_version" and resource.get(
                        "name") in names:
                    yield resource["values"]["secret_string"]
            for child in module.get("child_modules", []):
                yield from walk(child)

        return list(walk(state["values"]["root_module"]))

    def _connection_string(self) -> str:
        values = self._state_secrets(("db_connection", "connection_string"))
        if len(values) != 1:
            raise ProvisionError("cell must have exactly one database connection secret")
        return values[0]

    def seed_database(self, sql: str) -> dict:
        """Use this cell's existing connection secret in memory; never persist or log it."""
        parts = dict(part.split("=", 1) for part in self._connection_string().split(";") if "=" in part)
        parts = {k.strip().lower(): v for k, v in parts.items()}
        connection = {"host": parts["host"], "port": int(parts.get("port", "5432")),
            "databaseName": parts["database"], "username": parts["username"],
            "password": parts["password"], "provider": "PostGIS", "sslRequired": True,
            "sslMode": "Require"}
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        env.update(PGHOST=connection["host"], PGPORT=str(connection["port"]),
            PGDATABASE=connection["databaseName"], PGUSER=connection["username"],
            PGPASSWORD=connection["password"], PGSSLMODE="require", PGCONNECT_TIMEOUT="30")
        result = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1"], input=sql, text=True,
                                env=env, capture_output=True, timeout=120)
        if result.returncode:
            raise ProvisionError("cloud fixture SQL failed")
        return connection


    # --- pre-serving migration (serverless) ------------------------------------------------------
    MIGRATE_PORT = 18080
    MIGRATE_ATTEMPTS = 60
    MIGRATE_DELAY_SECONDS = 5.0
    _PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "DOCKER_HOST", "DOCKER_CONFIG")

    def migrate(self, redis_enabled: bool = False, *, run=subprocess.run, probe=None,
                sleep=time.sleep) -> dict:
        """Migrate the cell's database with the generic server image, then stop it.

        The image runs on this runner (the cell's RDS admits the runner's /32) with migrations
        enabled and the cell's own connection string, admin password and master key, until
        /healthz/ready answers 200. Secrets reach docker through the process environment
        (`-e NAME` with no value), never argv. Raises ProvisionError with the reason; the container
        is always removed.
        """
        image = os.environ.get(self.spec.migrate_image_env, "").strip()
        if not image:
            raise ProvisionError(f"{self.name}: {self.spec.migrate_image_env} is unset; cannot migrate")
        connection = self._connection_string()
        master_keys = self._state_secrets(("master_key",))
        admin = self.admin_api_key
        name = f"honua-migrate-{self.name}-{'r' if redis_enabled else 'n'}-{self.run_id}"[:63]
        env = {k: os.environ[k] for k in self._PASSTHROUGH_ENV if k in os.environ}
        server_env = {
            "HONUA_SKIP_MIGRATIONS": "false",
            "ConnectionStrings__DefaultConnection": connection,
            "HONUA_ADMIN_PASSWORD": admin,
            "Licensing__Mode": "Disabled",
            "HostValidation__AllowedHosts": "*",
            "AllowedHosts": "*",
        }
        if len(master_keys) == 1:
            server_env["Security__ConnectionEncryption__MasterKey"] = master_keys[0]
        env.update(server_env)
        hidden = [v for v in (connection, admin, *master_keys) if v]
        probe = probe or self._probe_ready
        url = f"http://127.0.0.1:{self.MIGRATE_PORT}/healthz/ready"
        result: dict = {"image": image, "path": "/healthz/ready", "ready": False, "attempts": 0}
        started = run(["docker", "run", "-d", "--name", name,
                       "-p", f"127.0.0.1:{self.MIGRATE_PORT}:8080",
                       *(arg for key in server_env for arg in ("-e", key)), image],
                      text=True, capture_output=True, env=env, check=False)
        try:
            if started.returncode:
                raise ProvisionError(f"{self.name}: migration container did not start: "
                                     + self._redact(started.stderr or started.stdout, hidden))
            last = None
            for attempt in range(1, self.MIGRATE_ATTEMPTS + 1):
                result["attempts"] = attempt
                last = probe(url)
                if last == 200:
                    result.update(ready=True, status=200)
                    return result
                state = run(["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", name],
                            text=True, capture_output=True, check=False)
                running, _, code = (state.stdout or "").strip().partition(" ")
                if state.returncode == 0 and running == "false":
                    raise ProvisionError(f"{self.name}: migration container exited {code or '?'} before "
                                         "Ready: " + self._container_tail(name, run, hidden))
                if attempt < self.MIGRATE_ATTEMPTS:
                    sleep(self.MIGRATE_DELAY_SECONDS)
            raise ProvisionError(f"{self.name}: migration never reported Ready at /healthz/ready "
                                 f"(last status {last}) after {self.MIGRATE_ATTEMPTS} probes: "
                                 + self._container_tail(name, run, hidden))
        finally:
            run(["docker", "rm", "-f", name], text=True, capture_output=True, check=False)

    @staticmethod
    def _probe_ready(url: str) -> int:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - loopback only
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except OSError:
            return 0

    @staticmethod
    def _redact(text: str, hidden: list[str]) -> str:
        text = (text or "").strip()
        for value in hidden:
            text = text.replace(value, "[redacted]")
        text = re.sub(r"(?i)(password|pwd|masterkey|api[-_]?key)(\s*[=:]\s*)[^;\s\"']+",
                      r"\1\2[redacted]", text)
        return text[-2000:]

    def _container_tail(self, name: str, run, hidden: list[str]) -> str:
        logs = run(["docker", "logs", "--tail", "20", name], text=True, capture_output=True, check=False)
        return self._redact((logs.stdout or "") + (logs.stderr or ""), hidden) or "no container output"


# With Redis connected outside Development/Test the server enables the durable operation secret
# channel and exits at startup unless Operations:SecretChannel:KeyRingCertificatePath names a
# certificate. Both AWS roots take it from an operator-owned Secrets Manager secret and refuse a Redis
# plan without the ARN: examples/aws injects it through ECS task secrets (honua-iac#216), and
# examples/aws-serverless hands the Lambda an aws:secretsmanager: reference the server resolves with
# the function role. Only the ARN crosses into Terraform; the KMS key is needed only for a
# customer-managed key. Passed on Redis-on cells only, and only when the pinned root declares them.
_OPERATION_KEY_RING_ENV_VARS = (
    ("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", "operation_key_ring_certificate_secret_arn"),
    ("HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN", "operation_key_ring_certificate_secret_kms_key_arn"),
)
_OPERATION_KEY_RING_REQUIRED_VARS = ("operation_key_ring_certificate_secret_arn",)

# Without AuditLog:ChainVerification:Key the server still serves and writes audit rows, but scheduled
# hash-chain verification never succeeds and its audit-chain-integrity health check is Unhealthy. The
# roots take an operator-owned Secrets Manager secret (ECS task secrets; a Lambda/Batch
# aws:secretsmanager: reference). Recommended, not required: passed on every cell when set and
# declared, never refused when unset.
_AUDIT_CHAIN_KEY_ENV_VARS = (
    ("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN", "audit_chain_key_secret_arn"),
    ("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_KMS_KEY_ARN", "audit_chain_key_secret_kms_key_arn"),
)

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
    # The 2026.1 GA serverless cell is Lambda + AWS Batch geoprocessing (rc.3 fix unit C2). Passed
    # only when the pinned root declares them (honua-iac feat/lambda-batch-ga-cell exposes them on
    # examples/aws-serverless); the release account pre-creates AWSServiceRoleForBatch.
    # image_repository_policy_mode=reuse: the cell role's guardrail (ProtectMirrorRepo) denies
    # ecr:SetRepositoryPolicy on the shared honua-server mirror, whose owner-installed policy already
    # authorizes Lambda retrieval; "owned" mode (the module default) fails provisioning with
    # AccessDenied, which is what every nightly aws-serverless cell has recorded.
    declared_ephemeral_vars=("enable_gp_batch=true", "use_batch_service_linked_role=true",
                             "image_repository_policy_mode=reuse"),
    # The manifest's generic server image, amd64 child by digest (resolved by e2e-cloud-aws.yml).
    env_vars=(("HONUA_GP_BATCH_IMAGE", "gp_batch_image"),),
    # Redis-on Lambda cells: the operation key-ring certificate secret (see above). An iac pin whose
    # serverless root predates these variables declares neither, so nothing is passed or refused.
    redis_env_vars=_OPERATION_KEY_RING_ENV_VARS,
    redis_required_vars=_OPERATION_KEY_RING_REQUIRED_VARS,
    optional_env_vars=_AUDIT_CHAIN_KEY_ENV_VARS,
    migrate_image_env="HONUA_MIGRATE_IMAGE",
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
    declared_ephemeral_vars=("rds_deletion_protection=false", "enable_postgis=true"),
    # Genuine-model cell only (rc.3 fix unit C6): the workflow sets HONUA_ENABLE_BEDROCK_AI=true for
    # aws-ecs/redis-off when its genuine_model_bedrock input is on; every other run stays cost-free.
    opt_in_env="HONUA_ENABLE_BEDROCK_AI",
    opt_in_vars=("enable_bedrock_ai=true", "bedrock_ai_region=us-east-1"),
    # honua-iac#216: the operation key-ring certificate secret for Redis-on cells (see above).
    redis_env_vars=_OPERATION_KEY_RING_ENV_VARS,
    redis_required_vars=_OPERATION_KEY_RING_REQUIRED_VARS,
    optional_env_vars=_AUDIT_CHAIN_KEY_ENV_VARS,
    ephemeral_var_files=("e2e/terraform/aws-ecs-new-deployment.tfvars.json",),
    needs_runner_db_access=True,
    needs_runner_alb_access=True,
    architecture_env="HONUA_ECS_ARCHITECTURE",
    architecture_var="task_cpu_architecture",
    # honua-release#450: per-run HTTPS hostname and the demo pages' origin (top-demo, S9).
    cell_domain=True,
    cors_allowed_origins=(DEMO_SITE_ORIGIN,),
)


def serverless(**kw) -> TerraformTarget:
    return TerraformTarget(SERVERLESS_SPEC, **kw)


def ecs(**kw) -> TerraformTarget:
    return TerraformTarget(ECS_SPEC, **kw)
