"""AWS EKS deploy target — the heaviest cell (k8s control plane + Helm + LoadBalancer).

Unlike serverless/ECS, EKS does not expose the app URL as a terraform output: terraform stands up the
cluster, the honua-helm chart is installed onto it (with Redis toggled via the chart's own value), and
the app endpoint is the provisioned LoadBalancer hostname. So this target needs the full k8s toolchain
(aws + kubectl + helm + the manifest-pinned chart) on top of terraform/AWS/image, and is
correspondingly run least often (TEST-STRATEGY: "EKS weekly, not nightly; high + slow").

Shape of one cell, mirroring what the ECS cell gets from the honua-iac aws-ecs module:

  1. terraform apply the honua-iac aws-eks example root — VPC + cluster + managed node group. The API
     server is published to the ephemeral runner's /32 ONLY (HONUA_AWS_RUNNER_CIDR), and the creating
     OIDC role is granted a cluster admin access entry so kubectl/helm can drive it.
  2. A PostGIS fixture Deployment in the cell namespace. The chart's bundled PostgreSQL subchart is
     development-only and has NO PostGIS, and the aws-eks root provisions no RDS, so the datastore is
     an in-cluster PostGIS pinned to the same engine family the local-docker seam tier certifies.
  3. helm upgrade --install of the manifest-pinned chart, with the exact manifest-pinned server image
     (by digest), an externally managed Secret holding the runtime credentials, `service.type
     =LoadBalancer`, and `redis.enabled` carrying the cell's Redis dimension — the chart's own Redis
     subchart, so the redis-on cell exercises the chart's Redis path rather than a bypass.
  4. Wait for the Service's LoadBalancer hostname, restrict the load balancer to the runner /32, and
     poll it until the candidate serves — that hostname is the endpoint the canonical checks and the
     canary probes run against.

Teardown is ordered and fail-closed: every LoadBalancer Service is deleted (and waited on) BEFORE
terraform destroys the VPC, because a surviving ELB holds the subnets/ENIs and strands the whole VPC
(honua-iac#142's orphan class), and a destroy that does not complete raises instead of being swallowed.
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from .base import Availability, DeployTarget, ProvisionError

EKS_ROOT = "infrastructure/terraform/examples/aws-eks"
NAMESPACE = "honua-cert"
RELEASE = "honua"
SECRET_NAME = "honua-runtime"
REDIS_RELEASE = "honua-redis"

# PostGIS fixture: the same engine family the local-docker seam tier (e2e/local-docker) certifies
# against, so the EKS cell's datastore is not a third, untested variant.
POSTGIS_IMAGE = "postgis/postgis:16-3.4"

# The chart's Redis subchart is the upstream Bitnami chart, whose pinned image tag was withdrawn from
# the `bitnami` Docker Hub namespace when Bitnami retired its free catalogue; the identical images
# remain published under `bitnamilegacy`. Pointing the subchart there is the only way to install the
# chart's OWN Redis path today (every chart version inside honua's `>=18 <21` dependency range points
# at a withdrawn tag), so the redis-on cell keeps exercising the chart instead of a hand-rolled
# bypass. Tracked for honua-helm: the chart should re-pin or replace the dependency.
REDIS_IMAGE_REPOSITORY = "bitnamilegacy/redis"


class AwsEksTarget(DeployTarget):
    name = "aws-eks"
    supports_redis = True

    def __init__(self, *, run_id: str = "local", region: str | None = None) -> None:
        self.run_id = run_id
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._workdir: Path | None = None
        self._prefix: str | None = None
        self._cluster_name: str | None = None
        self._kubeconfig: Path | None = None
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
        # provision so teardown reaps the exact same names it applied.
        redis_tag = "r" if redis_enabled else "n"
        return f"honuaeks{redis_tag}{self.run_id[:6]}".lower()[:18]

    def _iac_root(self) -> Path | None:
        base = os.environ.get("HONUA_IAC_DIR")
        if not base:
            return None
        root = Path(base) / EKS_ROOT
        return root if root.is_dir() else None

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

    def _redact(self, text: str) -> str:
        """Never let a generated credential reach the public Actions log through an error/diagnostic."""
        for secret_value in (self._db_password, self._admin_password,
                             self._master_key, self._redis_password):
            text = text.replace(secret_value, "***")
        return text

    def _failure(self, operation: str, error: subprocess.CalledProcessError) -> ProvisionError:
        detail = (error.stderr or error.stdout or str(error)).strip()
        return ProvisionError(f"{self.name} {operation} failed: {self._redact(detail)}")

    def _diagnostics(self) -> str:
        """Bounded, redacted cluster state captured while a failed cell is still alive."""
        if self._kubeconfig is None:
            return ""
        sections: list[str] = []
        for title, args in (
            ("pods", ("get", "pods", "-n", NAMESPACE, "-o", "wide")),
            ("events", ("get", "events", "-n", NAMESPACE, "--sort-by=.lastTimestamp")),
            ("honua log tail", ("logs", f"deployment/{RELEASE}", "-n", NAMESPACE,
                                "--all-containers=true", "--tail=80")),
        ):
            result = self._kubectl(*args, check=False)
            body = (result.stdout or result.stderr or "").strip()
            if body:
                sections.append(f"--- {title} ---\n{body[-3000:]}")
        return self._redact("\n".join(sections))

    # --- terraform lifecycle -------------------------------------------------------------------
    def _tf_vars(self, redis_enabled: bool) -> list[str]:
        prefix = self._prefix or self._name_prefix(redis_enabled)
        runner_cidr = self._runner_cidr()
        if runner_cidr is None:
            raise ProvisionError(
                f"{self.name}: HONUA_AWS_RUNNER_CIDR must be the runner's single IPv4 /32"
            )
        return [
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

    # --- kubernetes fixtures -------------------------------------------------------------------
    def _apply(self, manifest: dict) -> None:
        try:
            self._kubectl("apply", "-f", "-", input_text=json.dumps(manifest))
        except subprocess.CalledProcessError as error:
            raise self._failure(f"kubectl apply ({manifest.get('kind')})", error) from error

    def _install_database_fixture(self) -> None:
        """PostGIS + the runtime Secret. The chart's PostgreSQL subchart has no PostGIS and the
        aws-eks root provisions no RDS, so the cell's datastore lives in the cluster."""
        self._apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
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
        values = {
            "ConnectionStrings__DefaultConnection": (
                f"Host=postgis;Port=5432;Database=honua;Username=honua;"
                f"Password={self._db_password};SSL Mode=Disable"
            ),
            "HONUA_ADMIN_PASSWORD": self._admin_password,
            "Security__ConnectionEncryption__MasterKey": self._master_key,
        }
        if redis_enabled:
            # The chart's own Redis subchart, named deterministically below so the connection string
            # can be written before the release exists.
            values["ConnectionStrings__redis"] = (
                f"{REDIS_RELEASE}-master:6379,password={self._redis_password}"
            )
        self._apply({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": SECRET_NAME, "namespace": NAMESPACE},
            "type": "Opaque",
            "stringData": values,
        })

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

    def _helm_command(self, redis_enabled: bool, chart: Path) -> list[str]:
        repository, tag, digest = self._image_values(os.environ["HONUA_ECS_IMAGE"])
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
            # The chart's pre-install reachability hook cannot run for either cell here, and neither
            # reason is something this harness can configure around:
            #   * redis-off — the hook treats Redis as MANDATORY for every non-development
            #     environment, so it fails on the missing connection string before anything is
            #     installed. Whether the platform behaves correctly WITHOUT its cache is precisely
            #     what this dimension exists to certify, so the cell cannot supply one.
            #   * redis-on  — the hook is a pre-install hook, so it TCP-probes the chart's own Redis
            #     Service before the subchart that creates it exists.
            # It is a convenience pre-check over reachability, not part of the wire surface this tier
            # certifies: the canonical checks and canary probes run against the deployed candidate
            # either way. Tracked for honua-helm alongside the withdrawn Redis image above.
            "--set", "preflight.enabled=false",
            "--set", f"redis.enabled={'true' if redis_enabled else 'false'}",
        ]
        if redis_enabled:
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
        try:
            self._run(self._helm_command(redis_enabled, chart), env=self._kube_env())
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
        prefix = self._prefix = self._name_prefix(redis_enabled)
        self._kubeconfig = Path(tempfile.gettempdir()) / f"{prefix}.kubeconfig"
        try:
            self._tf(root, "init", "-input=false", "-no-color")
            self._tf(root, "apply", "-auto-approve", *self._tf_vars(redis_enabled))
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

        self._install_database_fixture()
        self._install_runtime_secret(redis_enabled)
        self._install_chart(redis_enabled)
        hostname = self._load_balancer_hostname()
        self._restrict_load_balancer()
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

        destroy = self._tf(root, "destroy", "-auto-approve", *self._tf_vars(mode), check=False)
        if destroy.returncode != 0:
            detail = (destroy.stderr or destroy.stdout or "terraform destroy returned nonzero").strip()
            raise ProvisionError(f"{self.name} teardown failed: {self._redact(detail)}")
