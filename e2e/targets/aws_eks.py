"""AWS EKS deploy target: cluster + Helm chart + Service LoadBalancer, run as a GA cell.

Owner decisions 12 and 18 (2026-10-10) pull honua-release#203 (bring-your-own Kubernetes) into 2026.1,
so the EKS cell runs the same provision -> admit -> journey -> teardown chain as the ECS and Lambda
cells and is held to the same verdict. EKS does not expose the app URL as a terraform output:
terraform stands up the cluster and its data plane, this harness installs the manifest-pinned
honua-helm chart onto it, and the endpoint is the Service's load balancer (or the per-run HTTPS name
aliased to it).

Shape of one cell:

  1. terraform apply the honua-iac `examples/aws-eks` root. Every input beyond the cluster basics is
     passed only when the pinned root declares it (terraform rejects an undeclared -var), exactly as
     the TfTargetSpec cells do, so a pin that predates an input keeps working:
       * the Redis dimension (`enable_redis`, ElastiCache) plus, on Redis-on cells, the operator-owned
         operation key-ring certificate secret, which a declaring root requires;
       * RDS PostgreSQL with PostGIS (`enable_postgis=true`, deletion protection off, reachable from
         the runner's /32 for the PostGIS bootstrap and the fixture seed);
       * the audit hash-chain key secret, the cells' operation policy, licensing and CORS;
       * the per-run `<label>.cert.<parent>` HTTPS name (`domain_name` + `route53_zone_id`).
     The API server is published to the runner's /32 only, and the creating OIDC role gets a cluster
     admin access entry so kubectl/helm can drive it.
  2. The runtime Secret (`secret.create=false`): the connection string, admin password, master key and
     Redis connection string. On a root with RDS and ElastiCache they come from its sensitive
     `db_connection_string` / `redis_connection_string` outputs. An older root without RDS gets the
     legacy in-cluster PostGIS fixture and the chart's own Redis; that shape is development-only and
     cannot pass the journey's TLS datasource stage, which is the honest verdict for it.
  3. helm upgrade --install of the manifest-pinned chart with the exact manifest-pinned image (by
     digest), `service.type=LoadBalancer`, and a values file rendered from the root's outputs: the
     server environment the root computed (`chart_config_env`: ControlPlane__Kubernetes__*, the
     operation policy rules, secret references), the server and GP job service accounts the root's
     workload identity binds (Pod Identity by default, so no role-arn annotation), the Kubernetes Job
     geoprocessing backend (when the pinned chart declares `geoprocessing.kubernetesJobs`), and TLS on
     the load balancer with the root's ACM `certificate_arn` when the cell has an HTTPS name.
  4. Wait for the load balancer, restrict it to the runner /32, point the per-run name at it (a
     harness-owned Route53 record), and poll until the candidate serves.

Teardown is ordered and fail-closed: the LoadBalancer Services and the cell's DNS record are deleted
BEFORE terraform destroys the VPC (a surviving ELB holds the subnets/ENIs and strands the whole VPC,
honua-iac#142), a destroy that does not complete raises, and a completed destroy is then verified
read-only: the cluster, the VPC, its ENIs and load balancers, and the cell's certificate and records
must all be gone, or the cell is red.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import terraform_target as _tt
from .base import Availability, DeployTarget, ProvisionError

EKS_ROOT = "infrastructure/terraform/examples/aws-eks"

# honua-iac's aws-eks root variable that switches the cluster's secret-encryption CMK on/off. Named
# once here because BOTH the declared-variable probe and the `-var` flag have to agree on the spelling
# — a typo in either would silently stop disabling the key (honua-release#127).
SECRET_ENCRYPTION_VAR = "cluster_secret_encryption_enabled"

# The root's Redis toggle. The ECS and Lambda roots spell it `redis_enabled`; the EKS root takes
# `enable_redis`. The first one the pinned root declares carries the cell's Redis dimension.
REDIS_VARS = ("enable_redis", "redis_enabled")

# Root-declared inputs an ephemeral cell needs, passed only when declared (TfTargetSpec
# declared_ephemeral_vars): RDS must be destroyable and must carry PostGIS (the server's PostGIS
# preflight refuses to start otherwise).
DECLARED_EPHEMERAL_VARS = ("rds_deletion_protection=false", "enable_postgis=true")

# The root outputs this harness reads (`terraform output -json`, in memory, never printed). Each is
# used only when the pinned root declares it, so an older root keeps the legacy shape.
DB_OUTPUT = "db_connection_string"            # sensitive; RDS PostGIS, TLS required
REDIS_OUTPUT = "redis_connection_string"      # sensitive; ElastiCache, Redis-on cells only
CERTIFICATE_OUTPUT = "certificate_arn"        # the per-run name's ACM certificate
CHART_ENV_OUTPUT = "chart_config_env"         # map(string): non-secret server environment
SERVER_ROLE_OUTPUT = "server_role_arn"        # IAM role the server pods assume
GP_JOB_ROLE_OUTPUT = "gp_job_role_arn"        # IAM role the geoprocessing Job pods assume
# How those roles reach the pods (honua-iac#233). Under `pod_identity` (the root's default) an EKS Pod
# Identity association binds each role to <kubernetes_namespace>/<service account>, and the service
# accounts must carry NO eks.amazonaws.com/role-arn annotation: the AWS SDK tries the web-identity
# token first, and the role trusts no OIDC provider. Under `irsa` the annotation is required. Either
# way the harness annotates only from the root's *_service_account_annotations outputs, which are
# empty under pod_identity, and never builds a role-arn annotation itself.
WORKLOAD_IDENTITY_OUTPUT = "workload_identity_mode"
SERVER_SA_ANNOTATIONS_OUTPUT = "server_service_account_annotations"
GP_JOB_SA_ANNOTATIONS_OUTPUT = "gp_job_service_account_annotations"
SERVER_SA_OUTPUT = "server_service_account_name"
GP_JOB_SA_OUTPUT = "gp_job_service_account_name"
NAMESPACE_OUTPUT = "kubernetes_namespace"
# Environment names from chart_config_env land in the chart's config.env map. Anything that is not a
# plain environment name is refused rather than rendered.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}\Z")
IRSA_ANNOTATION = "eks.amazonaws.com/role-arn"
# The Kubernetes Job geoprocessing backend (honua-helm#77, honua-server#4719): run the manifest's
# generic server image, the same image the Lambda cell's Batch jobs run.
GP_IMAGE_ENV = "HONUA_GP_BATCH_IMAGE"

NAMESPACE = "honua-cert"
RELEASE = "honua"
SECRET_NAME = "honua-runtime"
REDIS_RELEASE = "honua-redis"
# The service accounts the chart renders with fullnameOverride=RELEASE: serviceAccount.name defaults to
# the fullname, and geoprocessing.kubernetesJobs.serviceAccount.name to "<fullname>-gp-job".
SERVER_SERVICE_ACCOUNT = RELEASE
GP_JOB_SERVICE_ACCOUNT = f"{RELEASE}-gp-job"
# The root's Pod Identity associations bind to these, so each is passed when the root declares it.
# Without kubernetes_namespace the root binds the roles in `honua` and the pods in honua-cert get no
# AWS credentials.
WORKLOAD_IDENTITY_VARS = (
    ("kubernetes_namespace", NAMESPACE),
    ("server_service_account_name", SERVER_SERVICE_ACCOUNT),
    ("gp_job_service_account_name", GP_JOB_SERVICE_ACCOUNT),
)
# In-tree AWS cloud provider annotations for a TLS listener on the Service's load balancer.
_LB_ANNOTATION = "service.beta.kubernetes.io/aws-load-balancer-"
DNS_RECORD_TTL = 60

# PostGIS fixture for a root without RDS: the same engine family the local-docker seam tier
# (e2e/local-docker) certifies against, so the legacy datastore is not a third, untested variant.
POSTGIS_IMAGE = "postgis/postgis:16-3.4"

# The chart's Redis subchart is the upstream Bitnami chart, whose pinned image tag was withdrawn from
# the `bitnami` Docker Hub namespace when Bitnami retired its free catalogue; the identical images
# remain published under `bitnamilegacy`. Pointing the subchart there is the only way to install the
# chart's OWN Redis path today (every chart version inside honua's `>=18 <21` dependency range points
# at a withdrawn tag), so the legacy redis-on cell keeps exercising the chart instead of a hand-rolled
# bypass. Tracked as honua-helm#62: the chart should re-pin or replace the dependency.
REDIS_IMAGE_REPOSITORY = "bitnamilegacy/redis"


def _repository(reference: str) -> str:
    """`registry/name` of an image reference, without its tag or digest."""
    image = reference.split("@", 1)[0]
    if image.rfind(":") > image.rfind("/"):
        image = image[:image.rfind(":")]
    return image


def _operation_policy_var() -> str:
    """The cells' operation policy (`operations_policy_rules=<HCL list>`), named once in
    terraform_target by honua-release#517, so every cell runs the identical Production policy."""
    return _tt.OPERATIONS_POLICY_RULES_VAR


class AwsEksTarget(DeployTarget):
    name = "aws-eks"
    supports_redis = True
    admission = "eks"

    def __init__(self, *, run_id: str = "local", region: str | None = None) -> None:
        self.run_id = run_id
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._workdir: Path | None = None
        self._prefix: str | None = None
        self._cluster_name: str | None = None
        self._kubeconfig: Path | None = None
        self._outputs: dict | None = None
        self._endpoint_host: str | None = None
        # Per-cell ephemeral credentials. The Actions run id is public, so it never participates in
        # credential generation. The literal prefix satisfies the chart's preflight complexity rules
        # (>=16 chars with upper/lower/digit/special; master key >=32) regardless of the random tail.
        self._db_password = f"Honua-Cert-Db-Aa1!{secrets.token_urlsafe(32)}"
        self._admin_password = f"Honua-Cert-Admin-Aa1!{secrets.token_urlsafe(32)}"
        self._master_key = f"Honua-Cert-Master-Aa1!{secrets.token_urlsafe(48)}"
        self._redis_password = f"Honua-Cert-Redis-Aa1!{secrets.token_urlsafe(32)}"

    # --- prerequisites -------------------------------------------------------------------------
    def _name_prefix(self, redis_enabled: bool) -> str:
        # Redis mode in the prefix so the redis-on and redis-off EKS cells (same run_id, run in parallel)
        # provision independent, non-colliding cluster resource names. Bounded to 18 chars. Stored on
        # provision so teardown reaps the exact same names it applied. The `honuaeks` stem is also the
        # IAM namespace the release role may create roles and policies in (release-cicd-guardrails).
        redis_tag = "r" if redis_enabled else "n"
        return f"honuaeks{redis_tag}{self.run_id[:6]}".lower()[:18]

    def _iac_root(self) -> Path | None:
        base = os.environ.get("HONUA_IAC_DIR")
        if not base:
            return None
        root = Path(base) / EKS_ROOT
        return root if root.is_dir() else None

    def _root_declares(self, variable: str) -> bool:
        """True when the honua-iac root this cell will apply actually declares `variable`.

        Terraform is fail-closed about input it was not told to expect: `-var=foo=...` for a variable
        the ROOT module does not declare is a hard error ("Value for undeclared variable"), not a
        warning. honua-release does not float honua-iac: the tree is checked out at the exact sha
        pinned in platform-manifest.yaml (`components.honua-iac.sha`), and a human can point
        HONUA_IAC_DIR at an even older working copy. So a var introduced on the iac side is NOT
        available to this harness the moment it is merged there — only once the pin moves.

        Rather than couple the two merges (and break every EKS cell during the window between them),
        the flag is emitted only when the checked-out root declares it. The probe is a cheap read of
        the root's own .tf files — it does not recurse into modules, because `-var` binds to root
        variables and nothing else. Absent tree, absent file, unreadable file => False, i.e. behave
        exactly as this harness did before the flag existed.
        """
        return self._root_has("variable", variable)

    def _root_outputs_declared(self, output: str) -> bool:
        """True when the pinned root declares `output` (same probe as _root_declares)."""
        return self._root_has("output", output)

    def _root_has(self, block: str, name: str) -> bool:
        root = self._iac_root()
        if root is None:
            return False
        pattern = re.compile(r'^\s*' + block + r'\s+"' + re.escape(name) + r'"\s*\{', re.MULTILINE)
        for tf_file in sorted(root.glob("*.tf")):
            try:
                body = tf_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if pattern.search(body):
                return True
        return False

    def _redis_var(self) -> str | None:
        return next((name for name in REDIS_VARS if self._root_declares(name)), None)

    def _uses_rds(self) -> bool:
        """The pinned root provisions RDS PostgreSQL with PostGIS (the GA datastore)."""
        return self._root_declares("enable_postgis")

    def _uses_elasticache(self) -> bool:
        """The pinned root provisions ElastiCache for the Redis-on cell."""
        return self._redis_var() is not None

    def _chart_root(self) -> Path | None:
        """The chart directory inside the manifest-pinned honua-helm checkout (repo root or /honua)."""
        base = os.environ.get("HONUA_HELM_DIR")
        if not base:
            return None
        root = Path(base)
        for candidate in (root / "honua", root):
            if (candidate / "Chart.yaml").is_file():
                return candidate
        return None

    def _chart_declares(self, dotted: str) -> bool:
        """True when the pinned chart's values.yaml declares the dotted key (e.g.
        `geoprocessing.kubernetesJobs`). The chart analogue of _root_declares: helm silently ignores
        unknown values, so a value the pinned chart does not know would be a claim nothing checks."""
        chart = self._chart_root()
        if chart is None:
            return False
        try:
            node = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return False
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    @staticmethod
    def _has_aws_creds() -> bool:
        return bool(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ROLE_ARN")
                    or os.environ.get("AWS_PROFILE") or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"))

    @staticmethod
    def _runner_cidr() -> str | None:
        """The ephemeral runner's own /32 — the ONLY CIDR allowed to reach the API server and the LB."""
        raw = os.environ.get("HONUA_AWS_RUNNER_CIDR", "").strip()
        if not raw:
            return None
        try:
            network = ipaddress.ip_network(raw, strict=True)
        except ValueError:
            return None
        if network.version != 4 or network.prefixlen != 32:
            return None
        return str(network)

    @property
    def admin_api_key(self) -> str:
        return self._admin_password

    def availability(self) -> Availability:
        missing: list[str] = []
        for tool in ("aws", "terraform", "kubectl", "helm"):
            if not shutil.which(tool):
                missing.append(f"{tool} CLI")
        if not self._has_aws_creds():
            missing.append("AWS credentials (OIDC role / AWS_* env)")
        if not os.environ.get("HONUA_ECS_IMAGE"):
            missing.append("HONUA_ECS_IMAGE (container image for the k8s deployment)")
        if self._iac_root() is None:
            missing.append("HONUA_IAC_DIR pointing at the honua-iac terraform tree")
        if self._chart_root() is None:
            missing.append("HONUA_HELM_DIR pointing at the honua-helm chart")
        if self._runner_cidr() is None:
            missing.append("HONUA_AWS_RUNNER_CIDR (ephemeral runner /32 for API-server + LB ingress)")
        if self._uses_rds() and not shutil.which("psql"):
            missing.append("psql (fixture seed against the cell's RDS database)")
        if self._chart_declares("geoprocessing.kubernetesJobs") and not os.environ.get(GP_IMAGE_ENV):
            # Like the Lambda cell's Batch image: a chart that runs GP as Kubernetes Jobs must run the
            # pinned generic image, never the chart's fallback.
            missing.append(f"{GP_IMAGE_ENV} (pinned generic server image for Kubernetes GP jobs)")
        if missing:
            return Availability(False, f"{self.name} not runnable: " + "; ".join(missing), missing)
        return Availability(True, f"{self.name} prerequisites present")

    # --- process plumbing ----------------------------------------------------------------------
    def _run(self, command: list[str], *, input_text: str | None = None,
             env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(command, input=input_text, text=True, capture_output=True,
                              env=env, check=check)

    def _tf(self, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run(["terraform", f"-chdir={root}", *args], check=check)

    def _kube_env(self) -> dict[str, str]:
        if self._kubeconfig is None:
            raise ProvisionError(f"{self.name}: kubeconfig is not initialized")
        env = os.environ.copy()
        env["KUBECONFIG"] = str(self._kubeconfig)
        return env

    def _kubectl(self, *args: str, input_text: str | None = None,
                 check: bool = True) -> subprocess.CompletedProcess:
        return self._run(["kubectl", *args], input_text=input_text, env=self._kube_env(), check=check)

    def _aws(self, *args: str) -> subprocess.CompletedProcess:
        """A read or write against this cell's region, JSON out, never raising."""
        return self._run(["aws", *args, "--region", self.region, "--output", "json"], check=False)

    def _connection_string(self) -> str:
        """The cell's RDS connection string, from the root's sensitive output, in memory only."""
        value = self._output(DB_OUTPUT)
        if not isinstance(value, str) or not value:
            raise ProvisionError(f"{self.name}: the root output {DB_OUTPUT} is empty")
        return value

    def seed_database(self, sql: str) -> dict:
        if self._uses_rds():
            # The ECS cell's seed, unchanged: psql from this runner (the RDS admits its /32) with
            # TLS required, credentials only in the child's environment.
            return _tt.TerraformTarget.seed_database(self, sql)
        self._kubectl("exec", "-i", "deployment/postgis", "-n", NAMESPACE, "--",
                      "psql", "-U", "honua", "-d", "honua", "-v", "ON_ERROR_STOP=1",
                      input_text=sql)
        return {"host": "postgis", "port": 5432, "databaseName": "honua",
                "username": "honua", "password": self._db_password,
                "provider": "PostGIS", "sslRequired": False, "sslMode": "Disable"}

    def _redact(self, text: str) -> str:
        """Never let a generated credential reach the public Actions log through an error/diagnostic."""
        hidden = [self._db_password, self._admin_password, self._master_key, self._redis_password]
        for name in (DB_OUTPUT, REDIS_OUTPUT):
            value = (self._outputs or {}).get(name)
            if isinstance(value, str) and value:
                hidden.append(value)
        for secret_value in hidden:
            text = text.replace(secret_value, "***")
        return re.sub(r"(?i)(password|pwd)(\s*=\s*)[^;,\s\"']+", r"\1\2***", text)

    def _failure(self, operation: str, error: subprocess.CalledProcessError) -> ProvisionError:
        detail = (error.stderr or error.stdout or str(error)).strip()
        return ProvisionError(f"{self.name} {operation} failed: {self._redact(detail)}")

    def _diagnostics(self) -> str:
        """Bounded, redacted cluster state captured while a failed cell is still alive."""
        if self._kubeconfig is None:
            return ""
        sections: list[str] = []
        for title, body in self.diagnostic_sections():
            sections.append(f"--- {title} ---\n{body[-3000:]}")
        return "\n".join(sections)

    def diagnostic_sections(self) -> list[tuple[str, str]]:
        """(title, redacted text) of the namespace's pods, events and server log tail."""
        sections: list[tuple[str, str]] = []
        for title, args in (
            ("pods", ("get", "pods", "-n", NAMESPACE, "-o", "wide")),
            ("events", ("get", "events", "-n", NAMESPACE, "--sort-by=.lastTimestamp")),
            ("honua log tail", ("logs", f"deployment/{RELEASE}", "-n", NAMESPACE,
                                "--all-containers=true", "--tail=300")),
            ("jobs", ("get", "jobs", "-n", NAMESPACE, "-o", "wide")),
        ):
            result = self._kubectl(*args, check=False)
            body = (result.stdout or result.stderr or "").strip()
            if body:
                sections.append((title, self._redact(body)))
        return sections

    # --- terraform lifecycle -------------------------------------------------------------------
    def _tf_vars(self, redis_enabled: bool, *, destroy: bool = False) -> list[str]:
        prefix = self._prefix or self._name_prefix(redis_enabled)
        runner_cidr = self._runner_cidr()
        if runner_cidr is None:
            raise ProvisionError(
                f"{self.name}: HONUA_AWS_RUNNER_CIDR must be the runner's single IPv4 /32"
            )
        values = [
            "-input=false", "-no-color",
            f"-var=region={self.region}",
            f"-var=name_prefix={prefix}",
            "-var=environment=it",
            # The runner drives kubectl/helm from outside the VPC, so the API server must be public —
            # but ONLY to this runner, and only for the life of the cell.
            "-var=cluster_endpoint_public_access=true",
            f"-var=cluster_endpoint_public_access_cidrs={json.dumps([runner_cidr])}",
            # The OIDC role that applies this is the identity that must be able to install the chart.
            "-var=enable_cluster_creator_admin_permissions=true",
        ]
        # No customer-managed KMS key for an ephemeral cell (honua-release#127). EKS envelope-encrypts
        # Kubernetes Secrets with a CMK, and `terraform destroy` cannot delete that key — it can only
        # SCHEDULE deletion, and AWS's minimum window is 7 days with no way to shorten it. The key
        # keeps billing (~$1/key/month) for that whole week after the cell it belonged to is gone.
        # No parity assertion depends on it (see e2e/README.md "Cells leave nothing billing"); if the
        # cells ever should certify it, set the root's `cluster_secret_encryption_key_arn` to one
        # long-lived CMK created outside the harness instead of re-enabling the per-cell key.
        if self._root_declares(SECRET_ENCRYPTION_VAR):
            values.append(f"-var={SECRET_ENCRYPTION_VAR}=false")
        # Owner decision 9: the run id (and cell, and ceiling) on every resource, for Cost Explorer.
        if self._root_declares("tags"):
            from cost_meter import run_tags
            tags = run_tags(self.run_id, f"{self.name}/redis-{'on' if redis_enabled else 'off'}",
                            os.environ.get("HONUA_CLOUD_COST_CEILING_USD"))
            values.append(f"-var=tags={json.dumps(tags, separators=(',', ':'))}")
        redis_var = self._redis_var()
        if redis_var:
            values.append(f"-var={redis_var}={'true' if redis_enabled else 'false'}")
        if self._root_declares("licensing_mode"):
            # 2026.1 ships licensing disabled (honua-release#338), as on the ECS and Lambda cells.
            values.append("-var=licensing_mode=Disabled")
        values.extend(f"-var={v}" for v in DECLARED_EPHEMERAL_VARS
                      if self._root_declares(v.split("=", 1)[0]))
        values.extend(f"-var={variable}={value}" for variable, value in WORKLOAD_IDENTITY_VARS
                      if self._root_declares(variable))
        policy = _operation_policy_var()
        if self._root_declares(policy.split("=", 1)[0]):
            values.append(f"-var={policy}")
        if self._uses_rds():
            # The root's PostGIS bootstrap and this harness's fixture seed reach RDS from the runner,
            # exactly as on the ECS cell (needs_runner_db_access). Each input only when declared.
            if self._root_declares("db_publicly_accessible"):
                values.append("-var=db_publicly_accessible=true")
            if self._root_declares("db_additional_ingress_cidrs"):
                values.append("-var=db_additional_ingress_cidrs="
                              + json.dumps([runner_cidr], separators=(",", ":")))
        # The audit hash-chain key reaches every cell when set and declared; never refused when unset.
        values.extend(f"-var={variable}={os.environ[env_name].strip()}"
                      for env_name, variable in _tt._AUDIT_CHAIN_KEY_ENV_VARS
                      if os.environ.get(env_name, "").strip() and self._root_declares(variable))
        if redis_enabled:
            # With Redis connected outside Development/Test the server refuses to start without the
            # operation key-ring certificate; a declaring root refuses a Redis plan without it.
            declared = [(env_name, variable) for env_name, variable in _tt._OPERATION_KEY_RING_ENV_VARS
                        if self._root_declares(variable)]
            values.extend(f"-var={variable}={os.environ[env_name].strip()}" for env_name, variable in declared
                          if os.environ.get(env_name, "").strip())
            unset = [env_name for env_name, variable in declared
                     if variable in _tt._OPERATION_KEY_RING_REQUIRED_VARS
                     and not os.environ.get(env_name, "").strip()]
            if unset and not destroy:
                raise ProvisionError(
                    f"{self.name}: the pinned root requires {', '.join(unset)} on a Redis-on cell (an "
                    "operator-owned Secrets Manager secret holding the operation key-ring certificate; "
                    "owner step in e2e/README.md)")
        if self._root_declares("cors_allowed_origins"):
            values.append("-var=cors_allowed_origins=" + json.dumps([_tt.DEMO_SITE_ORIGIN], separators=(",", ":")))
        values.extend(self._domain_vars(redis_enabled, destroy=destroy))
        return values

    # --- per-run HTTPS cell hostname (honua-release#450, as on the ECS cells) ---------------------
    def cell_domain_label(self, redis_enabled: bool) -> str:
        """`<run id>-aws-eks-redis-<on|off>`, under the same rules as the ECS cells' label."""
        return _tt.TerraformTarget.cell_domain_label(self, redis_enabled)

    def _domain_configured(self) -> tuple[str, str] | None:
        """(zone id, parent) from the repository variables the ECS cells use, or None when unset.
        One without the other, or a malformed value, refuses (the ECS rule)."""
        zone = os.environ.get(_tt.CELL_DNS_ZONE_ENV, "").strip()
        parent = os.environ.get(_tt.CELL_DNS_PARENT_ENV, "").strip().lower().rstrip(".")
        if not zone and not parent:
            return None
        if not zone or not parent:
            missing = _tt.CELL_DNS_ZONE_ENV if not zone else _tt.CELL_DNS_PARENT_ENV
            raise ProvisionError(f"{self.name}: {missing} is unset while the other cell DNS variable is "
                                 "set; set both (owner step in e2e/README.md) or neither")
        if not _tt._ZONE_ID.fullmatch(zone):
            raise ProvisionError(f"{self.name}: {_tt.CELL_DNS_ZONE_ENV} is not a Route53 hosted zone id")
        if not _tt._DNS_NAME.fullmatch(parent):
            raise ProvisionError(f"{self.name}: {_tt.CELL_DNS_PARENT_ENV} is not a DNS name")
        return zone, parent

    def cell_domain(self, redis_enabled: bool) -> tuple[str, str] | None:
        """(fqdn, hosted zone id) for this cell, or None.

        None when the repository variables are unset, or when the pinned root does not yet declare
        `domain_name` and `route53_zone_id` (then the cell keeps its plain-HTTP load balancer and the
        provision log says so). The teardown leftover check uses the same answer, so it looks for
        exactly what the apply could have created.
        """
        configured = self._domain_configured()
        if configured is None:
            return None
        if not (self._root_declares("domain_name") and self._root_declares("route53_zone_id")):
            return None
        zone, parent = configured
        return f"{self.cell_domain_label(redis_enabled)}.{_tt.CELL_DNS_SUBDOMAIN}.{parent}", zone

    def _domain_vars(self, redis_enabled: bool, *, destroy: bool) -> list[str]:
        try:
            domain = self.cell_domain(redis_enabled)
        except ProvisionError:
            if destroy:
                return []  # destroy removes what the state holds whatever the inputs; it never refuses
            raise
        if domain is None:
            return []
        fqdn, zone = domain
        return [f"-var=domain_name={fqdn}", f"-var=route53_zone_id={zone}"]

    # Verbatim the ECS teardown's check: the cell's own certificate and records must be gone after a
    # successful destroy, other `*.cert.<parent>` names are a warning, and an unreadable listing fails.
    _cell_dns_leftovers = _tt.TerraformTarget._cell_dns_leftovers

    def _record_sets(self, zone: str, fqdn: str) -> list[dict]:
        listing = self._run(["aws", "route53", "list-resource-record-sets", "--hosted-zone-id", zone,
                             "--start-record-name", fqdn, "--max-items", "10", "--output", "json"],
                            check=False)
        if listing.returncode:
            raise ProvisionError(f"{self.name}: could not read the hosted zone records for {fqdn}")
        return [record for record in json.loads(listing.stdout or "{}").get("ResourceRecordSets", [])
                if str(record.get("Name", "")).lower().rstrip(".") == fqdn]

    def _change_record(self, zone: str, action: str, record: dict) -> None:
        batch = json.dumps({"Comment": f"honua-release#203 {self.name} cell",
                            "Changes": [{"Action": action, "ResourceRecordSet": record}]})
        result = self._run(["aws", "route53", "change-resource-record-sets", "--hosted-zone-id", zone,
                            "--change-batch", batch, "--output", "json"], check=False)
        if result.returncode:
            raise ProvisionError(f"{self.name}: Route53 {action} of {record['Name']} failed: "
                                 + self._redact((result.stderr or result.stdout or "").strip()))
        change = (json.loads(result.stdout or "{}").get("ChangeInfo") or {}).get("Id")
        if change and action != "DELETE":
            self._run(["aws", "route53", "wait", "resource-record-sets-changed", "--id", change],
                      check=False)

    def _point_cell_name(self, fqdn: str, zone: str, hostname: str) -> None:
        """The cell's name follows the Service's load balancer. The load balancer is created by the
        cluster, not by terraform, so this record is the harness's to create and delete; its
        certificate (and the certificate's validation records) are the root's."""
        self._change_record(zone, "UPSERT", {"Name": fqdn, "Type": "CNAME", "TTL": DNS_RECORD_TTL,
                                             "ResourceRecords": [{"Value": hostname}]})

    def _delete_cell_record(self, redis_enabled: bool) -> None:
        """Delete the harness-owned CNAME (exactly as it exists) before the destroy."""
        try:
            domain = self.cell_domain(redis_enabled)
        except ProvisionError:
            return
        if domain is None:
            return
        fqdn, zone = domain
        for record in self._record_sets(zone, fqdn):
            if record.get("Type") == "CNAME":
                self._change_record(zone, "DELETE", record)

    # --- kubernetes fixtures -------------------------------------------------------------------
    def _apply(self, manifest: dict) -> None:
        try:
            self._kubectl("apply", "-f", "-", input_text=json.dumps(manifest))
        except subprocess.CalledProcessError as error:
            raise self._failure(f"kubectl apply ({manifest.get('kind')})", error) from error

    def _ensure_namespace(self) -> None:
        self._apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})

    def _install_database_fixture(self) -> None:
        """LEGACY (a root without RDS): an in-cluster PostGIS. Development-shaped — no TLS, no
        durability — so it cannot pass the journey's TLS datasource stage."""
        self._ensure_namespace()
        self._apply({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "honua-postgis", "namespace": NAMESPACE},
            "type": "Opaque",
            "stringData": {"POSTGRES_PASSWORD": self._db_password},
        })
        self._apply({
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "postgis", "namespace": NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "postgis"}},
                "template": {
                    "metadata": {"labels": {"app": "postgis"}},
                    "spec": {
                        "containers": [{
                            "name": "postgis",
                            "image": POSTGIS_IMAGE,
                            "ports": [{"containerPort": 5432}],
                            "env": [
                                {"name": "POSTGRES_DB", "value": "honua"},
                                {"name": "POSTGRES_USER", "value": "honua"},
                                {"name": "POSTGRES_PASSWORD", "valueFrom": {"secretKeyRef": {
                                    "name": "honua-postgis", "key": "POSTGRES_PASSWORD"}}},
                                {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                            ],
                            "readinessProbe": {
                                "exec": {"command": ["pg_isready", "-U", "honua", "-d", "honua"]},
                                "initialDelaySeconds": 5, "periodSeconds": 5,
                            },
                            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                                          "limits": {"cpu": "1", "memory": "1Gi"}},
                            # The cell is ephemeral and the cluster has no CSI storage class, so the
                            # fixture's data lives for exactly as long as the cell does.
                            "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}],
                        }],
                        "volumes": [{"name": "data", "emptyDir": {}}],
                    },
                },
            },
        })
        self._apply({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "postgis", "namespace": NAMESPACE},
            "spec": {"selector": {"app": "postgis"},
                     "ports": [{"port": 5432, "targetPort": 5432}]},
        })
        try:
            self._kubectl("rollout", "status", "deployment/postgis", "-n", NAMESPACE, "--timeout=5m")
            self._kubectl(
                "exec", "deployment/postgis", "-n", NAMESPACE, "--",
                "psql", "-U", "honua", "-d", "honua", "-v", "ON_ERROR_STOP=1", "-c",
                "CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS postgis_raster;",
            )
        except subprocess.CalledProcessError as error:
            raise self._failure("PostGIS readiness/bootstrap", error) from error

    def _install_runtime_secret(self, redis_enabled: bool) -> None:
        """The chart's externally managed Secret path (`secret.create=false`).

        The chart REQUIRES a Redis connection string for any non-development environment when it
        manages the Secret itself, so a Production redis-off install is only expressible through an
        external Secret — which is also how the ECS cell gets its credentials (Secrets Manager), and
        keeps them out of the Helm release values."""
        if self._uses_rds():
            connection = self._connection_string()
        else:
            connection = (f"Host=postgis;Port=5432;Database=honua;Username=honua;"
                          f"Password={self._db_password};SSL Mode=Disable")
        values = {
            "ConnectionStrings__DefaultConnection": connection,
            "HONUA_ADMIN_PASSWORD": self._admin_password,
            "Security__ConnectionEncryption__MasterKey": self._master_key,
        }
        if redis_enabled:
            if self._uses_elasticache():
                redis = self._output(REDIS_OUTPUT)
                if not isinstance(redis, str) or not redis:
                    raise ProvisionError(f"{self.name}: a Redis-on cell needs the root output "
                                         f"{REDIS_OUTPUT} (ElastiCache), which is empty or undeclared")
                values["ConnectionStrings__redis"] = redis
            else:
                # LEGACY: the chart's own Redis subchart, named deterministically below so the
                # connection string can be written before the release exists.
                values["ConnectionStrings__redis"] = (
                    f"{REDIS_RELEASE}-master:6379,password={self._redis_password}"
                )
        self._ensure_namespace()
        self._apply({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": SECRET_NAME, "namespace": NAMESPACE},
            "type": "Opaque",
            "stringData": values,
        })

    # --- root outputs --------------------------------------------------------------------------
    def _load_outputs(self) -> dict:
        """`terraform output -json`, flattened to name -> value. Held in memory, never printed."""
        if self._outputs is None:
            if self._workdir is None:
                raise ProvisionError(f"{self.name}: terraform outputs read before apply")
            try:
                raw = json.loads(self._tf(self._workdir, "output", "-json").stdout or "{}")
            except subprocess.CalledProcessError as error:
                raise ProvisionError(f"{self.name}: terraform output failed") from error
            except ValueError as error:
                raise ProvisionError(f"{self.name}: terraform output was not JSON") from error
            self._outputs = {name: (entry or {}).get("value") for name, entry in raw.items()}
        return self._outputs

    def _output(self, name: str):
        """The root output `name`, or None when the pinned root does not declare it."""
        if not self._root_outputs_declared(name):
            return None
        return self._load_outputs().get(name)

    # --- helm ----------------------------------------------------------------------------------
    @staticmethod
    def _image_values(reference: str) -> tuple[str, str, str]:
        """Split the manifest-pinned image into the chart's (repository, tag, digest) values."""
        image, separator, digest = reference.partition("@")
        last_slash = image.rfind("/")
        last_colon = image.rfind(":")
        tag = ""
        if last_colon > last_slash:
            image, tag = image[:last_colon], image[last_colon + 1:]
        if separator:
            # Digest-pinned: the chart renders repository@digest, so the tag is dropped rather than
            # rendered alongside it. The digest IS the pin.
            return image, "", digest
        if not tag:
            raise ProvisionError(f"HONUA_ECS_IMAGE must include a tag or digest: {reference}")
        return image, tag, ""

    def chart_values(self, redis_enabled: bool) -> dict:
        """The values file rendered from the root's outputs and the pinned chart's declared keys.

        Kept apart from the `--set` flags so a value carrying commas, dots or colons (ARNs, policy
        reasons) is never re-parsed by helm's `--set` grammar. No credential is rendered here: the
        runtime Secret holds those.
        """
        env: dict[str, str] = {
            # The demo pages' origin (top-demo, S9), as the ECS cells pass cors_allowed_origins.
            "Cors__AllowedOrigins__0": _tt.DEMO_SITE_ORIGIN,
        }
        rendered = self._output(CHART_ENV_OUTPUT)
        if rendered is not None:
            if not isinstance(rendered, dict):
                raise ProvisionError(f"{self.name}: root output {CHART_ENV_OUTPUT} must be a map of strings")
            for key, value in rendered.items():
                if not _ENV_NAME.match(str(key)) or not isinstance(value, (str, int, float, bool)):
                    raise ProvisionError(f"{self.name}: root output {CHART_ENV_OUTPUT} holds a value "
                                         f"that is not a plain environment entry ({str(key)[:64]!r})")
                env[str(key)] = str(value).lower() if isinstance(value, bool) else str(value)
        values: dict = {"config": {"env": env}}
        self._check_identity_binding()
        server_account = self._service_account_values(
            SERVER_SERVICE_ACCOUNT, SERVER_SA_ANNOTATIONS_OUTPUT, SERVER_ROLE_OUTPUT)
        if server_account:
            values["serviceAccount"] = server_account
        if self._chart_declares("geoprocessing.kubernetesJobs"):
            repository, tag, digest = self._image_values(os.environ.get(GP_IMAGE_ENV, ""))
            jobs: dict = {"enabled": True, "namespace": NAMESPACE,
                          "image": {"repository": repository, "tag": tag, "digest": digest}}
            job_account = self._service_account_values(
                GP_JOB_SERVICE_ACCOUNT, GP_JOB_SA_ANNOTATIONS_OUTPUT, GP_JOB_ROLE_OUTPUT)
            if job_account:
                jobs["serviceAccount"] = job_account
            values["geoprocessing"] = {"kubernetesJobs": jobs}
        domain = self.cell_domain(redis_enabled)
        if domain is not None:
            fqdn, _ = domain
            certificate = self._output(CERTIFICATE_OUTPUT)
            if not certificate:
                raise ProvisionError(f"{self.name}: the cell has an HTTPS name ({fqdn}) but the pinned "
                                     f"root declares no {CERTIFICATE_OUTPUT} output to terminate it with")
            values["service"] = {"port": 443, "annotations": {
                _LB_ANNOTATION + "ssl-cert": str(certificate),
                _LB_ANNOTATION + "ssl-ports": "443",
                _LB_ANNOTATION + "backend-protocol": "http",
            }}
            env["Public__BaseUrl"] = f"https://{fqdn}"
        return values

    def _workload_identity_mode(self) -> str | None:
        mode = self._output(WORKLOAD_IDENTITY_OUTPUT)
        if mode is None:
            return None
        if mode not in ("pod_identity", "irsa"):
            raise ProvisionError(f"{self.name}: root output {WORKLOAD_IDENTITY_OUTPUT} must be "
                                 f"pod_identity or irsa, not {str(mode)[:32]!r}")
        return mode

    def _check_identity_binding(self) -> None:
        """The root bound its roles to the namespace and accounts the chart is installed with.

        A Pod Identity association names one namespace/account pair, so a mismatch would leave the
        pods without AWS credentials. It refuses provisioning instead of failing later in the journey.
        """
        expected = ((NAMESPACE_OUTPUT, NAMESPACE), (SERVER_SA_OUTPUT, SERVER_SERVICE_ACCOUNT),
                    (GP_JOB_SA_OUTPUT, GP_JOB_SERVICE_ACCOUNT))
        for output, value in expected:
            bound = self._output(output)
            if bound is not None and bound != value:
                raise ProvisionError(f"{self.name}: the root binds its workload role to {output}="
                                     f"{str(bound)[:64]!r}, but the chart is installed with {value!r}")

    def _service_account_values(self, name: str, annotations_output: str, role_output: str) -> dict:
        """The chart's serviceAccount values for one account: its name and the root's annotations.

        Only the root's annotations output is rendered. Under pod_identity it must not carry the IRSA
        role-arn annotation; under irsa it must, for the root's role.
        """
        if not self._root_outputs_declared(annotations_output):
            return {}
        annotations = self._output(annotations_output) or {}
        if not isinstance(annotations, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in annotations.items()):
            raise ProvisionError(f"{self.name}: root output {annotations_output} must be a map of strings")
        mode = self._workload_identity_mode()
        if mode == "pod_identity" and IRSA_ANNOTATION in annotations:
            raise ProvisionError(f"{self.name}: {annotations_output} carries {IRSA_ANNOTATION} under "
                                 "pod_identity; the SDK would try web identity against a role that "
                                 "trusts no OIDC provider")
        if mode == "irsa":
            role = self._output(role_output)
            if not annotations.get(IRSA_ANNOTATION) or (role and annotations[IRSA_ANNOTATION] != role):
                raise ProvisionError(f"{self.name}: under irsa {annotations_output} must carry "
                                     f"{IRSA_ANNOTATION} for the root's {role_output}")
        values: dict = {"name": name}
        if annotations:
            values["annotations"] = dict(annotations)
        return values

    def _helm_command(self, redis_enabled: bool, chart: Path, values_file: Path | None = None) -> list[str]:
        repository, tag, digest = self._image_values(os.environ["HONUA_ECS_IMAGE"])
        elasticache = self._uses_elasticache()
        values = [
            "helm", "upgrade", "--install", RELEASE, str(chart),
            "--namespace", NAMESPACE,
            "--wait", "--timeout", "15m",
            "--set-string", f"fullnameOverride={RELEASE}",
            # The EXACT manifest-pinned candidate — resolved by the candidate job, never hardcoded.
            "--set-string", f"image.repository={repository}",
            "--set-string", f"image.tag={tag}",
            "--set-string", f"image.digest={digest}",
            "--set", "image.pullPolicy=IfNotPresent",
            # The cell's endpoint: a real AWS load balancer in front of the chart's Service, which is
            # what the canonical checks and canary probes are pointed at.
            "--set", "service.type=LoadBalancer",
            # Credentials come from the Secret installed above, not from the release values.
            "--set", "secret.create=false",
            "--set-string", f"secret.name={SECRET_NAME}",
            # The chart's PostgreSQL subchart is development-only and carries no PostGIS.
            "--set", "postgresql.enabled=false",
            # The chart's pre-install reachability hook cannot run for either cell here:
            #   * redis-off — the hook treats Redis as MANDATORY for every non-development
            #     environment, so it fails before anything is installed. Whether the platform behaves
            #     correctly WITHOUT its cache is precisely what this dimension exists to certify.
            #   * redis-on  — (chart Redis) the hook probes the chart's own Redis Service before the
            #     subchart that creates it exists.
            # It is a convenience pre-check over reachability, not part of the wire surface this tier
            # certifies. Tracked as honua-helm#62.
            "--set", "preflight.enabled=false",
            # PARITY WITH THE ECS CELL: honua-iac's aws example root passes exactly these through
            # `additional_env`. Host validation is off because the plain-HTTP endpoint IS a generated
            # load-balancer DNS name, which the server otherwise rejects with 400 "Invalid Host header".
            "--set-string", "config.env.HostValidation__Enabled=false",
            "--set-string", "config.env.Licensing__Mode=Disabled",
            "--set-string", "config.env.HONUA_SERVE_ADMIN_UI=true",
            "--set-string", "config.env.HONUA_ADMIN_UI=true",
            # ElastiCache (root-provisioned) replaces the chart's Redis whenever the root declares it.
            "--set", f"redis.enabled={'true' if redis_enabled and not elasticache else 'false'}",
        ]
        if redis_enabled and not elasticache:
            values += [
                "--set-string", f"redis.fullnameOverride={REDIS_RELEASE}",
                "--set", "redis.auth.enabled=true",
                "--set-string", f"redis.auth.password={self._redis_password}",
                # No CSI driver is installed on this ephemeral cluster, so a PVC would never bind.
                "--set", "redis.master.persistence.enabled=false",
                "--set-string", f"redis.image.repository={REDIS_IMAGE_REPOSITORY}",
                # Required by the Bitnami subchart whenever its image is not the withdrawn default.
                "--set", "global.security.allowInsecureImages=true",
            ]
        if values_file is not None:
            values += ["-f", str(values_file)]
        return values

    def _install_chart(self, redis_enabled: bool) -> None:
        chart = self._chart_root()
        if chart is None or not os.environ.get("HONUA_ECS_IMAGE"):
            raise ProvisionError(f"{self.name}: chart or image pin disappeared after the availability check")
        try:
            # honua-helm does not vendor its subchart archives, so the manifest-pinned checkout has an
            # empty charts/ directory and the chart cannot even load without them. `dependency build`
            # honours Chart.lock exactly (`update` would re-resolve it).
            for index, repository in enumerate(self._chart_dependency_repositories(chart), start=1):
                self._run(["helm", "repo", "add", f"honua-chart-dependency-{index}", repository,
                           "--force-update"])
            self._run(["helm", "dependency", "build", str(chart)])
        except subprocess.CalledProcessError as error:
            raise self._failure("Helm dependency build", error) from error
        with tempfile.TemporaryDirectory(prefix="honua-eks-values-") as scratch:
            values_file = Path(scratch) / "values.json"
            values_file.write_text(json.dumps(self.chart_values(redis_enabled), indent=2), encoding="utf-8")
            try:
                self._run(self._helm_command(redis_enabled, chart, values_file), env=self._kube_env())
            except subprocess.CalledProcessError as error:
                failure = self._failure("Helm install", error)
                diagnostics = self._diagnostics()
                if diagnostics:
                    raise ProvisionError(f"{failure}\n{diagnostics}") from error
                raise failure from error

    @staticmethod
    def _chart_dependency_repositories(chart: Path) -> list[str]:
        chart_yaml = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8")) or {}
        repositories = {
            str(dependency.get("repository", ""))
            for dependency in (chart_yaml.get("dependencies") or [])
            if str(dependency.get("repository", "")).startswith(("http://", "https://"))
        }
        return sorted(repositories)

    # --- endpoint ------------------------------------------------------------------------------
    def _load_balancer_hostname(self, timeout_seconds: float = 600.0) -> str:
        deadline = time.monotonic() + timeout_seconds
        last = ""
        while time.monotonic() < deadline:
            result = self._kubectl("get", "service", RELEASE, "-n", NAMESPACE, "-o", "json", check=False)
            if result.returncode == 0:
                try:
                    service = json.loads(result.stdout)
                except json.JSONDecodeError:
                    service = {}
                for ingress in (service.get("status", {}).get("loadBalancer", {}).get("ingress") or []):
                    hostname = ingress.get("hostname") or ingress.get("ip")
                    if hostname:
                        return str(hostname)
            else:
                last = (result.stderr or result.stdout or "").strip()
            time.sleep(10)
        raise ProvisionError(
            f"{self.name}: the chart's Service never published a LoadBalancer hostname "
            f"within {int(timeout_seconds)}s{f' ({self._redact(last)})' if last else ''}"
        )

    def _restrict_load_balancer(self) -> None:
        """Only the runner that certifies this cell may reach its load balancer."""
        runner_cidr = self._runner_cidr()
        if runner_cidr is None:
            raise ProvisionError(f"{self.name}: HONUA_AWS_RUNNER_CIDR disappeared before LB restriction")
        patch = json.dumps({"spec": {"loadBalancerSourceRanges": [runner_cidr]}})
        try:
            self._kubectl("patch", "service", RELEASE, "-n", NAMESPACE, "-p", patch)
        except subprocess.CalledProcessError as error:
            raise self._failure("LoadBalancer source-range restriction", error) from error

    def grant_operator(self, *, redis_enabled: bool, timeout_seconds: float = 1500.0) -> None:
        """Let THIS runner drive kubectl: add its own /32 to the cluster's public API CIDRs.

        The admit and teardown jobs run on fresh runners (honua-release#381), and the API server was
        published to the provisioning runner alone. Each grant is still a single /32.
        """
        runner_cidr = self._runner_cidr()
        if runner_cidr is None:
            raise ProvisionError(f"{self.name}: HONUA_AWS_RUNNER_CIDR must be this runner's single IPv4 /32")
        prefix = self._prefix = self._prefix or self._name_prefix(redis_enabled)
        self._cluster_name = self._cluster_name or f"{prefix}-it-eks"
        self._kubeconfig = self._kubeconfig or Path(tempfile.gettempdir()) / f"{prefix}.kubeconfig"
        region = ["--name", self._cluster_name, "--region", self.region]
        try:
            current = json.loads(self._run(["aws", "eks", "describe-cluster", *region, "--query",
                "cluster.resourcesVpcConfig.publicAccessCidrs", "--output", "json"]).stdout) or []
            if runner_cidr not in current:
                config = json.dumps({"endpointPublicAccess": True,
                                     "publicAccessCidrs": [*current, runner_cidr]})
                update_id = self._run(["aws", "eks", "update-cluster-config", *region,
                    "--resources-vpc-config", config, "--query", "update.id",
                    "--output", "text"]).stdout.strip()
                deadline = time.monotonic() + timeout_seconds
                while True:
                    status = self._run(["aws", "eks", "describe-update", *region, "--update-id",
                        update_id, "--query", "update.status", "--output", "text"]).stdout.strip()
                    if status == "Successful":
                        break
                    if status in ("Failed", "Cancelled") or time.monotonic() >= deadline:
                        raise ProvisionError(f"{self.name}: cluster API access update ended {status}")
                    time.sleep(15)
            self._run(["aws", "eks", "update-kubeconfig", *region, "--kubeconfig", str(self._kubeconfig)])
        except subprocess.CalledProcessError as error:
            raise self._failure("cluster API access grant", error) from error

    def admit(self, endpoint: str, cidr: str, *, redis_enabled: bool = False) -> None:
        """Add the credential-free journey runner's /32 to the chart Service's source ranges."""
        try:
            network = ipaddress.ip_network(cidr.strip(), strict=True)
        except ValueError as error:
            raise ProvisionError(f"{self.name}: the journey runner CIDR is not valid") from error
        if network.version != 4 or network.prefixlen != 32:
            raise ProvisionError(f"{self.name}: the journey runner CIDR must be a single IPv4 /32")
        self.grant_operator(redis_enabled=redis_enabled)
        try:
            service = json.loads(self._kubectl("get", "service", RELEASE, "-n", NAMESPACE, "-o", "json").stdout)
            ranges = list((service.get("spec") or {}).get("loadBalancerSourceRanges") or [])
            if str(network) not in ranges:
                patch = json.dumps({"spec": {"loadBalancerSourceRanges": [*ranges, str(network)]}})
                self._kubectl("patch", "service", RELEASE, "-n", NAMESPACE, "-p", patch)
        except subprocess.CalledProcessError as error:
            raise self._failure("journey runner admission", error) from error

    def observed_image(self, pinned: dict) -> str | None:
        """The pinned server image only if every server pod of the release runs it.

        Read from the pods' container statuses (the digest the kubelet pulled), never inferred from
        the helm inputs; the EKS analogue of cloud_journey.observed_ecs_image. Anything else is None.
        """
        server = pinned["components"]["honua-server"]
        listing = self._kubectl("get", "pods", "-n", NAMESPACE, "-l",
                                f"app.kubernetes.io/instance={RELEASE},app.kubernetes.io/component=server",
                                "-o", "json", check=False)
        if listing.returncode:
            return None
        pods = json.loads(listing.stdout or "{}").get("items") or []
        if not pods:
            return None
        for pod in pods:
            status = pod.get("status") or {}
            containers = [c for c in status.get("containerStatuses") or []
                          if _repository(str(c.get("image", ""))) == server["image"]]
            if (status.get("phase") != "Running" or not containers
                    or any(not c.get("ready") or str(c.get("imageID", "")).rsplit("@", 1)[-1] != server["digest"]
                           for c in containers)):
                return None
        return f"{server['image']}@{server['digest']}"

    def _await_endpoint(self, url: str, timeout_seconds: float = 900.0) -> None:
        """An AWS load balancer answers before its DNS/target registration settles; wait for the
        candidate itself to serve, so a canonical check never races the load balancer's warm-up."""
        deadline = time.monotonic() + timeout_seconds
        last = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{url}/healthz/ready", timeout=10) as response:  # noqa: S310
                    if 200 <= response.status < 300:
                        return
                    last = f"HTTP {response.status}"
            except urllib.error.HTTPError as error:
                last = f"HTTP {error.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = str(error)
            time.sleep(10)
        diagnostics = self._diagnostics()
        raise ProvisionError(
            f"{self.name}: {url} did not become ready within {int(timeout_seconds)}s "
            f"(last: {last})" + (f"\n{diagnostics}" if diagnostics else "")
        )

    # --- lifecycle -----------------------------------------------------------------------------
    def provision(self, redis_enabled: bool = False) -> str:
        root = self._iac_root()
        if root is None:
            raise ProvisionError(f"{self.name}: honua-iac EKS root not found (set HONUA_IAC_DIR)")
        self._workdir = root
        self._outputs = None
        prefix = self._prefix = self._name_prefix(redis_enabled)
        self._kubeconfig = Path(tempfile.gettempdir()) / f"{prefix}.kubeconfig"
        tf_vars = self._tf_vars(redis_enabled)
        domain = self.cell_domain(redis_enabled)
        if domain is not None:
            print(f"{self.name}: HTTPS cell hostname {domain[0]} (ACM certificate from the root; the "
                  "harness points the name at the Service's load balancer)", flush=True)
        elif self._domain_configured() is not None:
            print(f"{self.name}: the pinned root does not declare domain_name/route53_zone_id; the cell "
                  "keeps its plain-HTTP load balancer endpoint (honua-release#203)", flush=True)
        if not self._uses_rds():
            print(f"{self.name}: the pinned root provisions no RDS (no enable_postgis); the cell runs the "
                  "development-only in-cluster PostGIS and cannot certify the TLS datasource "
                  "(honua-release#203)", flush=True)
        try:
            self._tf(root, "init", "-input=false", "-no-color")
            self._tf(root, "apply", "-auto-approve", *tf_vars)
            self._cluster_name = self._tf(root, "output", "-raw", "cluster_name").stdout.strip()
        except subprocess.CalledProcessError as error:
            raise self._failure("cluster terraform", error) from error
        if not self._cluster_name:
            raise ProvisionError(f"{self.name}: terraform applied but cluster_name was empty")
        try:
            self._run(["aws", "eks", "update-kubeconfig", "--name", self._cluster_name,
                       "--region", self.region, "--kubeconfig", str(self._kubeconfig)])
        except subprocess.CalledProcessError as error:
            raise self._failure("kubeconfig resolution", error) from error

        if not self._uses_rds():
            self._install_database_fixture()
        self._install_runtime_secret(redis_enabled)
        self._install_chart(redis_enabled)
        hostname = self._load_balancer_hostname()
        self._restrict_load_balancer()
        if domain is not None:
            fqdn, zone = domain
            self._point_cell_name(fqdn, zone, hostname)
            url = f"https://{fqdn}"
        else:
            url = f"http://{hostname}"
        self._await_endpoint(url)
        return url

    def _delete_load_balancer_services(self) -> None:
        """Every LoadBalancer Service must be gone BEFORE terraform destroys the VPC: the ELB and its
        managed security group hold the subnets, so a survivor strands the entire VPC. Deleting the
        Service blocks on the cloud-provider finalizer, i.e. on the ELB actually being deleted."""
        listing = self._kubectl("get", "services", "--all-namespaces", "-o", "json", check=False)
        if listing.returncode != 0:
            return
        try:
            items = json.loads(listing.stdout).get("items") or []
        except json.JSONDecodeError:
            return
        for service in items:
            if (service.get("spec") or {}).get("type") != "LoadBalancer":
                continue
            metadata = service.get("metadata") or {}
            self._kubectl("delete", "service", str(metadata.get("name")),
                          "-n", str(metadata.get("namespace")),
                          "--wait=true", "--timeout=10m", check=False)

    def _vpc_id(self, prefix: str) -> str | None:
        """The cell's VPC, resolved from the deterministic Name tag the module applies rather than
        from Terraform state, so the standalone backstop reaper can find it too."""
        result = self._run([
            "aws", "ec2", "describe-vpcs", "--region", self.region,
            "--filters", f"Name=tag:Name,Values={prefix}-it-eks-vpc",
            "--query", "Vpcs[].VpcId", "--output", "text",
        ], check=False)
        if result.returncode != 0:
            return None
        vpc_ids = result.stdout.split()
        return vpc_ids[0] if vpc_ids else None

    def _vpc_network_interfaces(self, vpc_id: str) -> list[tuple[str, str]]:
        result = self._run([
            "aws", "ec2", "describe-network-interfaces", "--region", self.region,
            "--filters", f"Name=vpc-id,Values={vpc_id}",
            "--query", "NetworkInterfaces[].[NetworkInterfaceId,Status]", "--output", "text",
        ], check=False)
        if result.returncode != 0:
            return []
        interfaces = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2:
                interfaces.append((fields[0], fields[1]))
        return interfaces

    def _sweep_detached_network_interfaces(self, prefix: str, timeout_seconds: float = 240.0) -> int:
        """Delete the ENIs EKS leaves behind, so the retried destroy can finish.

        The VPC CNI attaches secondary ENIs to every node. Deleting the managed node group detaches
        them but does NOT delete them, and a detached ENI still holds its subnet and its security
        group — so Terraform's subnet/security-group delete fails with DependencyViolation (after
        retrying for many minutes) and leaves the entire VPC behind. That is exactly honua-iac#142's
        orphan: the expensive resources are gone, the VPC keeps its quota slot forever, and the next
        run dies on VpcLimitExceeded. Every ENI in this VPC belongs to this run-scoped cell, so any
        one that is no longer attached is ours to remove.
        """
        vpc_id = self._vpc_id(prefix)
        if vpc_id is None:
            return 0
        swept = 0
        deadline = time.monotonic() + timeout_seconds
        while True:
            interfaces = self._vpc_network_interfaces(vpc_id)
            for interface_id, status in interfaces:
                if status != "available":
                    continue
                if self._run(["aws", "ec2", "delete-network-interface", "--region", self.region,
                              "--network-interface-id", interface_id], check=False).returncode == 0:
                    swept += 1
            still_attaching = [i for i, status in interfaces if status != "available"]
            if not still_attaching or time.monotonic() >= deadline:
                return swept
            time.sleep(15)

    def _listed(self, result: subprocess.CompletedProcess, key: str, what: str) -> list:
        if result.returncode:
            raise ProvisionError(f"{self.name} teardown could not verify {what} was removed: the "
                                 "read-only listing failed")
        try:
            return list(json.loads(result.stdout or "").get(key) or [])
        except (ValueError, AttributeError) as error:
            raise ProvisionError(f"{self.name} teardown could not verify {what} was removed: the "
                                 "listing was not JSON") from error

    def _verify_teardown(self, prefix: str, cluster: str, vpc_id: str | None, redis_enabled: bool) -> None:
        """After a successful destroy: nothing of this cell may remain. Read-only, fail-closed.

        The ECS teardown verifies its certificate and records; an EKS cell can also strand what
        terraform never managed — the Service's load balancer (and its security group), the VPC CNI's
        ENIs — or a cluster/VPC a partial destroy left. Each is listed; any leftover, or a listing that
        cannot be read, fails the cell (an unverified cleanup is not a verified one).
        """
        leftovers: list[str] = []
        described = self._aws("eks", "describe-cluster", "--name", cluster)
        if described.returncode == 0:
            leftovers.append(f"EKS cluster {cluster}")
        elif "ResourceNotFoundException" not in (described.stderr or ""):
            raise ProvisionError(f"{self.name} teardown could not verify EKS cluster {cluster} was removed")
        vpcs = self._listed(self._aws("ec2", "describe-vpcs", "--filters",
                                      f"Name=tag:Name,Values={prefix}-it-eks-vpc"), "Vpcs", "the VPC")
        leftovers += [f"VPC {v.get('VpcId')}" for v in vpcs]
        interfaces = self._listed(self._aws("ec2", "describe-network-interfaces", "--filters",
                                            f"Name=tag:cluster.k8s.amazonaws.com/name,Values={cluster}"),
                                  "NetworkInterfaces", "the cluster's ENIs")
        vpc_ids = {v.get("VpcId") for v in vpcs} | ({vpc_id} if vpc_id else set())
        for vpc in sorted(v for v in vpc_ids if v):
            interfaces += self._listed(self._aws("ec2", "describe-network-interfaces", "--filters",
                                                 f"Name=vpc-id,Values={vpc}"),
                                       "NetworkInterfaces", f"the ENIs of {vpc}")
        leftovers += sorted({f"ENI {i.get('NetworkInterfaceId')}" for i in interfaces})
        if vpc_ids - {None}:
            classic = self._listed(self._aws("elb", "describe-load-balancers"),
                                   "LoadBalancerDescriptions", "the Service load balancer")
            current = self._listed(self._aws("elbv2", "describe-load-balancers"),
                                   "LoadBalancers", "the Service load balancer")
            leftovers += [f"load balancer {b.get('LoadBalancerName')}" for b in classic
                          if b.get("VPCId") in vpc_ids]
            leftovers += [f"load balancer {b.get('LoadBalancerName')}" for b in current
                          if b.get("VpcId") in vpc_ids]
        if leftovers:
            raise ProvisionError(f"{self.name} teardown left {', '.join(leftovers)} behind")
        self._cell_dns_leftovers(redis_enabled, run=lambda command, **_: self._run(command, check=False))

    def teardown(self, redis_enabled: bool | None = None) -> None:
        root = self._workdir or self._iac_root()
        if root is None:
            return
        mode = False if redis_enabled is None else redis_enabled
        prefix = self._prefix = self._prefix or self._name_prefix(mode)
        # The standalone backstop reaper runs in a fresh process after a cancellation, so it
        # reconstructs the cluster/kubeconfig names this cell would have applied.
        self._cluster_name = self._cluster_name or f"{prefix}-it-eks"
        self._kubeconfig = self._kubeconfig or Path(tempfile.gettempdir()) / f"{prefix}.kubeconfig"
        kubeconfig = self._run(
            ["aws", "eks", "update-kubeconfig", "--name", self._cluster_name,
             "--region", self.region, "--kubeconfig", str(self._kubeconfig)],
            check=False,
        )
        if kubeconfig.returncode == 0:
            self._delete_load_balancer_services()
            self._run(["helm", "uninstall", RELEASE, "--namespace", NAMESPACE, "--wait",
                       "--timeout", "10m"], env=self._kube_env(), check=False)
            self._kubectl("delete", "namespace", NAMESPACE, "--wait=true", "--timeout=10m", check=False)
        # The cell's name points at the load balancer just deleted; the record is the harness's own.
        # A failure here never blocks the destroy: the leftover check below fails the cell instead.
        try:
            self._delete_cell_record(mode)
        except ProvisionError as error:
            print(f"::warning title=cell DNS record::{error}", flush=True)
        vpc_id = self._vpc_id(prefix)

        destroy = self._tf(root, "destroy", "-auto-approve", *self._tf_vars(mode, destroy=True), check=False)
        if destroy.returncode != 0:
            # One retry, and only after removing the specific thing that makes this destroy fail:
            # the node ENIs the VPC CNI leaks. Anything else is a genuine strand and stays red.
            swept = self._sweep_detached_network_interfaces(prefix)
            destroy = self._tf(root, "destroy", "-auto-approve", *self._tf_vars(mode, destroy=True), check=False)
            if destroy.returncode != 0:
                detail = (destroy.stderr or destroy.stdout or "terraform destroy returned nonzero").strip()
                raise ProvisionError(
                    f"{self.name} teardown failed after sweeping {swept} detached network "
                    f"interface(s): {self._redact(detail)}"
                )
        self._verify_teardown(prefix, self._cluster_name, vpc_id, mode)
