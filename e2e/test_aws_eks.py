"""Self-tests for the GA EKS cell (honua-release#203; owner decisions 12/18 of 2026-10-10).

The EKS cell must behave like the ECS and Lambda TfTargetSpec cells wherever the same concept exists:
every root input is passed only when the pinned honua-iac root declares it, a Redis-on cell refuses
without the operation key-ring secret, the datastore is the root's RDS PostGIS, the per-run HTTPS
name and the audit-chain key come from the same environment names, and teardown verifies the cell's
own leftovers read-only. No cloud, no terraform, no cluster: every external call is injected.

Run: python -m pytest e2e/test_aws_eks.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import run_cloud  # noqa: E402
from targets import aws_eks  # noqa: E402
from targets import terraform_target as tt  # noqa: E402
from targets.aws_eks import AwsEksTarget  # noqa: E402
from targets.base import ProvisionError  # noqa: E402

IMAGE = "ghcr.io/honua-io/honua-server@sha256:" + "a" * 64
GP_IMAGE = "ghcr.io/honua-io/honua-server@sha256:" + "b" * 64
RUNNER = "192.0.2.10/32"
KEY_RING = "arn:aws:secretsmanager:us-east-1:111111111111:secret:keyring"
AUDIT_KEY = "arn:aws:secretsmanager:us-east-1:111111111111:secret:audit"
ZONE = "Z089181827C9GKIKHXUTT"
POLICY = 'operations_policy_rules=[{operation_id="*",role="admin",decision="Allow"}]'

_ENV = ("HONUA_ECS_IMAGE", "HONUA_AWS_RUNNER_CIDR", "HONUA_IAC_DIR", "HONUA_HELM_DIR", "HONUA_GP_BATCH_IMAGE",
        "HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", "HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN",
        "HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN", "HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_KMS_KEY_ARN",
        "HONUA_AWS_CELL_DNS_ZONE_ID", "HONUA_AWS_CELL_DNS_PARENT")


@pytest.fixture
def cell(monkeypatch, tmp_path):
    """An EKS target over a throwaway iac root and chart. `cell.root(...)` writes the root's
    declarations; `cell.chart(...)` the chart's values.yaml."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HONUA_ECS_IMAGE", IMAGE)
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", RUNNER)
    iac = tmp_path / "iac"
    root = iac / aws_eks.EKS_ROOT
    root.mkdir(parents=True)
    monkeypatch.setenv("HONUA_IAC_DIR", str(iac))
    helm = tmp_path / "helm" / "honua"
    helm.mkdir(parents=True)
    (helm / "Chart.yaml").write_text("name: honua\nversion: 0.4.0\n", encoding="utf-8")
    (helm / "values.yaml").write_text("service:\n  type: ClusterIP\n", encoding="utf-8")
    monkeypatch.setenv("HONUA_HELM_DIR", str(tmp_path / "helm"))
    target = AwsEksTarget(run_id="38057015781")

    def declare(*variables, outputs=()):
        body = 'variable "region" {\n  type = string\n}\n'
        body += "".join(f'variable "{v}" {{\n  type = string\n}}\n' for v in variables)
        body += "".join(f'output "{o}" {{\n  value = "x"\n}}\n' for o in outputs)
        (root / "variables.tf").write_text(body, encoding="utf-8")
        target._outputs = None

    def chart(values: str):
        (helm / "values.yaml").write_text(values, encoding="utf-8")

    target.root = declare
    target.chart = chart
    declare()
    return target


def _vars(argv: list[str]) -> dict[str, str]:
    return dict(a[len("-var="):].split("=", 1) for a in argv if a.startswith("-var="))


def _outputs(target, **values):
    target._workdir = Path(".")
    target._outputs = values


# ---- root-declared terraform inputs ----------------------------------------------------------------
def test_an_old_root_gets_only_the_cluster_inputs(cell):
    values = _vars(cell._tf_vars(True))
    assert set(values) == {"region", "name_prefix", "environment", "cluster_endpoint_public_access",
                           "cluster_endpoint_public_access_cidrs", "enable_cluster_creator_admin_permissions"}
    # The IAM namespace the release role may create roles in (release-cicd-guardrails) is honuaeks*.
    assert values["name_prefix"].startswith("honuaeksr")


@pytest.mark.parametrize("variable", aws_eks.REDIS_VARS)
def test_the_redis_dimension_rides_whichever_toggle_the_root_declares(cell, variable):
    cell.root(variable)
    assert _vars(cell._tf_vars(True))[variable] == "true"
    assert _vars(cell._tf_vars(False))[variable] == "false"
    assert cell._uses_elasticache()


def test_enable_redis_wins_when_a_root_declares_both_spellings(cell):
    cell.root("enable_redis", "redis_enabled")
    values = _vars(cell._tf_vars(True))
    assert values["enable_redis"] == "true" and "redis_enabled" not in values


def test_rds_inputs_are_passed_only_when_the_root_declares_them(cell):
    cell.root("enable_postgis", "rds_deletion_protection", "db_publicly_accessible", "db_additional_ingress_cidrs")
    values = _vars(cell._tf_vars(False))
    assert values["enable_postgis"] == "true"
    assert values["rds_deletion_protection"] == "false"
    assert values["db_publicly_accessible"] == "true"
    assert json.loads(values["db_additional_ingress_cidrs"]) == [RUNNER]
    # A root with PostGIS RDS but no runner-ingress inputs gets neither.
    cell.root("enable_postgis")
    values = _vars(cell._tf_vars(False))
    assert "db_publicly_accessible" not in values and "rds_deletion_protection" not in values
    # Ingress inputs never open a database the root does not provision.
    cell.root("db_publicly_accessible", "db_additional_ingress_cidrs")
    assert not {"db_publicly_accessible", "db_additional_ingress_cidrs"} & set(_vars(cell._tf_vars(False)))


def test_licensing_cors_and_secret_encryption_follow_the_root(cell):
    cell.root("licensing_mode", "cors_allowed_origins", aws_eks.SECRET_ENCRYPTION_VAR)
    values = _vars(cell._tf_vars(False))
    assert values["licensing_mode"] == "Disabled"
    assert json.loads(values["cors_allowed_origins"]) == [tt.DEMO_SITE_ORIGIN]
    assert values[aws_eks.SECRET_ENCRYPTION_VAR] == "false"
    cell.root()
    assert not {"licensing_mode", "cors_allowed_origins"} & set(_vars(cell._tf_vars(False)))


def test_the_operation_policy_is_the_cells_shared_value_and_only_when_declared(cell, monkeypatch):
    # The ECS and Lambda cells' constant (honua-release#517), not a copy of it.
    assert f"-var={tt.OPERATIONS_POLICY_RULES_VAR}" not in cell._tf_vars(False)
    cell.root("operations_policy_rules")
    assert f"-var={tt.OPERATIONS_POLICY_RULES_VAR}" in cell._tf_vars(False)
    monkeypatch.setattr(tt, "OPERATIONS_POLICY_RULES_VAR", POLICY)
    for redis in (True, False):
        for destroy in (False, True):
            # One argv element carrying the whole HCL literal (no shell), apply and destroy alike.
            assert f"-var={POLICY}" in cell._tf_vars(redis, destroy=destroy)
    cell.root()
    assert not any(a.startswith("-var=operations_policy_rules") for a in cell._tf_vars(False))


def test_the_audit_chain_key_reaches_every_cell_when_set_and_declared(cell, monkeypatch):
    cell.root("audit_chain_key_secret_arn", "audit_chain_key_secret_kms_key_arn")
    assert "audit_chain_key_secret_arn" not in _vars(cell._tf_vars(False))  # unset: never refused
    monkeypatch.setenv("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN", AUDIT_KEY)
    for redis in (False, True):
        assert _vars(cell._tf_vars(redis))["audit_chain_key_secret_arn"] == AUDIT_KEY
    cell.root()
    assert "audit_chain_key_secret_arn" not in _vars(cell._tf_vars(False))


def test_a_redis_on_cell_requires_the_key_ring_secret_its_root_declares(cell, monkeypatch):
    cell.root("enable_redis", "operation_key_ring_certificate_secret_arn",
              "operation_key_ring_certificate_secret_kms_key_arn")
    with pytest.raises(ProvisionError, match="HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN"):
        cell._tf_vars(True)
    # Destroy never refuses: a cell that could not plan built nothing.
    assert "operation_key_ring_certificate_secret_arn" not in _vars(cell._tf_vars(True, destroy=True))
    # Redis-off never needs or receives it.
    assert "operation_key_ring_certificate_secret_arn" not in _vars(cell._tf_vars(False))
    monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING)
    values = _vars(cell._tf_vars(True))
    assert values["operation_key_ring_certificate_secret_arn"] == KEY_RING
    assert "operation_key_ring_certificate_secret_kms_key_arn" not in values  # optional, unset
    assert "operation_key_ring_certificate_secret_arn" not in _vars(cell._tf_vars(False))
    # A root that predates the input neither receives nor requires it.
    monkeypatch.delenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN")
    cell.root("enable_redis")
    assert "operation_key_ring_certificate_secret_arn" not in _vars(cell._tf_vars(True))


# ---- per-run HTTPS name -----------------------------------------------------------------------------
def _dns(monkeypatch, zone=ZONE, parent="demo.honua.io"):
    if zone is not None:
        monkeypatch.setenv("HONUA_AWS_CELL_DNS_ZONE_ID", zone)
    if parent is not None:
        monkeypatch.setenv("HONUA_AWS_CELL_DNS_PARENT", parent)


def test_the_cell_gets_its_per_run_name_under_the_ecs_cells_zone(cell, monkeypatch):
    _dns(monkeypatch)
    cell.root("domain_name", "route53_zone_id")
    values = _vars(cell._tf_vars(False))
    assert values["domain_name"] == "38057015781-aws-eks-redis-off.cert.demo.honua.io"
    assert values["route53_zone_id"] == ZONE
    assert _vars(cell._tf_vars(True))["domain_name"] == "38057015781-aws-eks-redis-on.cert.demo.honua.io"
    assert len(values["domain_name"]) <= 64


def test_a_root_without_domain_inputs_keeps_plain_http_rather_than_refusing(cell, monkeypatch):
    _dns(monkeypatch)
    assert cell.cell_domain(False) is None
    assert "domain_name" not in _vars(cell._tf_vars(False))


def test_half_configured_cell_dns_refuses_provision_but_never_destroy(cell, monkeypatch):
    _dns(monkeypatch, parent=None)
    cell.root("domain_name", "route53_zone_id")
    with pytest.raises(ProvisionError, match="HONUA_AWS_CELL_DNS_PARENT"):
        cell._tf_vars(False)
    assert "domain_name" not in _vars(cell._tf_vars(False, destroy=True))
    _dns(monkeypatch, zone="not-a-zone")
    with pytest.raises(ProvisionError, match="hosted zone id"):
        cell._tf_vars(False)


# ---- chart values rendered from the root's outputs ---------------------------------------------------
def test_chart_values_carry_the_root_rendered_server_environment(cell):
    cell.root(outputs=(aws_eks.CHART_ENV_OUTPUT, aws_eks.SERVER_ROLE_OUTPUT))
    _outputs(cell, chart_config_env={
        "ControlPlane__Kubernetes__DefaultNamespace": "honua-cert",
        "ControlPlane__Kubernetes__InClusterAutoDetect": True,
        "Operations__Policy__Rules__0__Role": "admin",
    }, server_role_arn="arn:aws:iam::111111111111:role/honuaeksr-server")
    values = cell.chart_values(False)
    env = values["config"]["env"]
    assert env["ControlPlane__Kubernetes__DefaultNamespace"] == "honua-cert"
    assert env["ControlPlane__Kubernetes__InClusterAutoDetect"] == "true"
    assert env["Operations__Policy__Rules__0__Role"] == "admin"
    assert env["Cors__AllowedOrigins__0"] == tt.DEMO_SITE_ORIGIN
    assert values["serviceAccount"]["annotations"][aws_eks.IRSA_ANNOTATION].endswith("honuaeksr-server")
    assert "geoprocessing" not in values and "service" not in values


def test_chart_values_ignore_outputs_the_root_does_not_declare(cell):
    _outputs(cell, chart_config_env={"Leak": "x"}, server_role_arn="arn:x")
    values = cell.chart_values(False)
    assert values == {"config": {"env": {"Cors__AllowedOrigins__0": tt.DEMO_SITE_ORIGIN}}}


@pytest.mark.parametrize("bad", [{"not an env name": "x"}, {"Ok": {"nested": "x"}}, ["Ok=x"]])
def test_chart_values_refuse_anything_but_plain_environment_entries(cell, bad):
    cell.root(outputs=(aws_eks.CHART_ENV_OUTPUT,))
    _outputs(cell, chart_config_env=bad)
    with pytest.raises(ProvisionError, match=aws_eks.CHART_ENV_OUTPUT):
        cell.chart_values(False)


def test_gp_runs_as_kubernetes_jobs_only_when_the_pinned_chart_declares_it(cell, monkeypatch):
    cell.root(outputs=(aws_eks.GP_JOB_ROLE_OUTPUT,))
    _outputs(cell, gp_job_role_arn="arn:aws:iam::111111111111:role/honuaeksr-gp-job")
    monkeypatch.setenv("HONUA_GP_BATCH_IMAGE", GP_IMAGE)
    assert "geoprocessing" not in cell.chart_values(False)
    cell.chart("geoprocessing:\n  kubernetesJobs:\n    enabled: false\n")
    jobs = cell.chart_values(False)["geoprocessing"]["kubernetesJobs"]
    assert jobs["enabled"] is True and jobs["namespace"] == aws_eks.NAMESPACE
    assert jobs["image"] == {"repository": "ghcr.io/honua-io/honua-server", "tag": "", "digest": "sha256:" + "b" * 64}
    assert jobs["serviceAccount"]["annotations"][aws_eks.IRSA_ANNOTATION].endswith("gp-job")


def test_a_chart_with_kubernetes_jobs_is_blocked_without_the_pinned_gp_image(cell):
    cell.chart("geoprocessing:\n  kubernetesJobs: {}\n")
    assert any("HONUA_GP_BATCH_IMAGE" in m for m in cell.availability().missing)


def test_an_https_cell_terminates_tls_on_the_load_balancer_with_the_roots_certificate(cell, monkeypatch):
    _dns(monkeypatch)
    cell.root("domain_name", "route53_zone_id", outputs=(aws_eks.CERTIFICATE_OUTPUT,))
    certificate = "arn:aws:acm:us-east-1:111111111111:certificate/abc"
    _outputs(cell, certificate_arn=certificate)
    values = cell.chart_values(True)
    annotations = values["service"]["annotations"]
    assert values["service"]["port"] == 443
    assert annotations["service.beta.kubernetes.io/aws-load-balancer-ssl-cert"] == certificate
    assert annotations["service.beta.kubernetes.io/aws-load-balancer-ssl-ports"] == "443"
    assert values["config"]["env"]["Public__BaseUrl"] == "https://38057015781-aws-eks-redis-on.cert.demo.honua.io"
    cell.root("domain_name", "route53_zone_id")
    with pytest.raises(ProvisionError, match="certificate_arn"):
        cell.chart_values(True)


def test_elasticache_replaces_the_charts_redis_and_the_values_file_is_passed(cell):
    cell.root("enable_redis")
    command = cell._helm_command(True, Path("/chart"), Path("/tmp/values.json"))
    sets = dict(pair.split("=", 1) for flag, pair in zip(command, command[1:]) if flag in ("--set", "--set-string"))
    assert sets["redis.enabled"] == "false"
    assert "redis.auth.password" not in sets and "global.security.allowInsecureImages" not in sets
    assert command[-2:] == ["-f", str(Path("/tmp/values.json"))]
    # A legacy root keeps the chart's own Redis on the Redis-on cell.
    cell.root()
    assert "redis.enabled=true" in cell._helm_command(True, Path("/chart"))


def test_the_runtime_secret_uses_the_roots_rds_and_elasticache(cell, monkeypatch):
    cell.root("enable_redis", "enable_postgis", outputs=(aws_eks.DB_OUTPUT, aws_eks.REDIS_OUTPUT))
    rds = "Host=db.example;Port=5432;Database=honua;Username=honua;Password=pw;SSL Mode=Require"
    _outputs(cell, db_connection_string=rds, redis_connection_string="cache:6379,password=pw,ssl=true")
    applied = []
    monkeypatch.setattr(cell, "_apply", applied.append)
    cell._install_runtime_secret(True)
    secret = applied[-1]["stringData"]
    assert secret["ConnectionStrings__DefaultConnection"] == rds
    assert secret["ConnectionStrings__redis"] == "cache:6379,password=pw,ssl=true"
    cell._install_runtime_secret(False)
    assert "ConnectionStrings__redis" not in applied[-1]["stringData"]
    # A Redis-on cell whose root declares ElastiCache but no connection output cannot be built.
    cell.root("enable_redis", "enable_postgis", outputs=(aws_eks.DB_OUTPUT,))
    _outputs(cell, db_connection_string=rds)
    with pytest.raises(ProvisionError, match=aws_eks.REDIS_OUTPUT):
        cell._install_runtime_secret(True)
    # Connection strings from the root never reach a failure message.
    assert "pw" not in cell._redact(f"failed: {rds}")


def test_the_seed_runs_against_rds_with_tls_when_the_root_provisions_it(cell, monkeypatch):
    cell.root("enable_postgis", outputs=(aws_eks.DB_OUTPUT,))
    _outputs(cell, db_connection_string="Host=db.example;Port=5432;Database=honua;Username=u;Password=pw")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, env=kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tt.subprocess, "run", fake_run)
    connection = cell.seed_database("select 1;")
    assert connection["host"] == "db.example" and connection["sslRequired"] is True
    assert seen["argv"][0] == "psql" and seen["env"]["PGSSLMODE"] == "require"
    assert "pw" not in " ".join(seen["argv"])


# ---- endpoint, image read-back ---------------------------------------------------------------------
def _pods(*statuses):
    return json.dumps({"items": [{"status": status} for status in statuses]})


def _pod(digest="a" * 64, ready=True, phase="Running"):
    return {"phase": phase, "containerStatuses": [{
        "name": "honua", "ready": ready, "image": "ghcr.io/honua-io/honua-server@sha256:" + digest,
        "imageID": "ghcr.io/honua-io/honua-server@sha256:" + digest}]}


@pytest.mark.parametrize("pods,expected", [
    ([_pod()], IMAGE),
    ([_pod(), _pod(digest="c" * 64)], None),
    ([_pod(ready=False)], None),
    ([_pod(phase="Pending")], None),
    ([], None),
])
def test_the_running_image_is_read_back_from_every_server_pod(cell, monkeypatch, pods, expected):
    pinned = {"components": {"honua-server": {"image": "ghcr.io/honua-io/honua-server",
                                              "digest": "sha256:" + "a" * 64}}}
    monkeypatch.setattr(cell, "_kubectl", lambda *a, **k: subprocess.CompletedProcess(a, 0, _pods(*pods), ""))
    assert cell.observed_image(pinned) == expected


def test_the_cell_name_is_pointed_at_the_load_balancer_and_deleted_before_destroy(cell, monkeypatch):
    _dns(monkeypatch)
    cell.root("domain_name", "route53_zone_id")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "list-resource-record-sets" in command:
            records = [{"Name": "38057015781-aws-eks-redis-off.cert.demo.honua.io.", "Type": "CNAME",
                        "TTL": 60, "ResourceRecords": [{"Value": "lb.example"}]}]
            return subprocess.CompletedProcess(command, 0, json.dumps({"ResourceRecordSets": records}), "")
        return subprocess.CompletedProcess(command, 0, json.dumps({"ChangeInfo": {"Id": "/change/1"}}), "")

    monkeypatch.setattr(cell, "_run", fake_run)
    cell._point_cell_name("38057015781-aws-eks-redis-off.cert.demo.honua.io", ZONE, "lb.example")
    upsert = json.loads(calls[0][calls[0].index("--change-batch") + 1])["Changes"][0]
    assert upsert["Action"] == "UPSERT" and upsert["ResourceRecordSet"]["Type"] == "CNAME"
    assert upsert["ResourceRecordSet"]["ResourceRecords"] == [{"Value": "lb.example"}]
    calls.clear()
    cell._delete_cell_record(False)
    changes = [json.loads(c[c.index("--change-batch") + 1])["Changes"][0] for c in calls if "--change-batch" in c]
    assert [c["Action"] for c in changes] == ["DELETE"]
    assert changes[0]["ResourceRecordSet"]["ResourceRecords"] == [{"Value": "lb.example"}]


# ---- teardown verification -------------------------------------------------------------------------
def _aws_world(*, cluster=False, vpcs=(), enis=(), classic=(), current=(), fail=None):
    """A fake `aws` for the post-destroy check: what still exists, and which listing fails."""
    def run(command, **kwargs):
        verb = " ".join(command[1:3])
        if fail and fail in verb:
            return subprocess.CompletedProcess(command, 255, "", "AccessDenied")
        if verb == "eks describe-cluster":
            if cluster:
                return subprocess.CompletedProcess(command, 0, "{}", "")
            return subprocess.CompletedProcess(command, 254, "", "ResourceNotFoundException: No cluster found")
        body = {
            "ec2 describe-vpcs": {"Vpcs": [{"VpcId": v} for v in vpcs]},
            "ec2 describe-network-interfaces": {"NetworkInterfaces": [{"NetworkInterfaceId": e} for e in enis]},
            "elb describe-load-balancers": {"LoadBalancerDescriptions": list(classic)},
            "elbv2 describe-load-balancers": {"LoadBalancers": list(current)},
        }.get(verb, {})
        return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
    return run


def test_a_clean_teardown_verifies(cell, monkeypatch):
    monkeypatch.setattr(cell, "_run", _aws_world())
    cell._verify_teardown("honuaeksn380570", "honuaeksn380570-it-eks", "vpc-0cell", False)


@pytest.mark.parametrize("world,leftover", [
    ({"cluster": True}, "EKS cluster"),
    ({"vpcs": ["vpc-0cell"]}, "VPC vpc-0cell"),
    ({"enis": ["eni-0leak"]}, "ENI eni-0leak"),
    ({"classic": [{"LoadBalancerName": "a1b2", "VPCId": "vpc-0cell"}]}, "load balancer a1b2"),
    ({"current": [{"LoadBalancerName": "k8s-nlb", "VpcId": "vpc-0cell"}]}, "load balancer k8s-nlb"),
])
def test_teardown_fails_on_any_leftover_of_the_cell(cell, monkeypatch, world, leftover):
    monkeypatch.setattr(cell, "_run", _aws_world(**world))
    with pytest.raises(ProvisionError, match=leftover):
        cell._verify_teardown("honuaeksn380570", "honuaeksn380570-it-eks", "vpc-0cell", False)


def test_another_vpcs_load_balancer_is_not_this_cells_leftover(cell, monkeypatch):
    monkeypatch.setattr(cell, "_run", _aws_world(classic=[{"LoadBalancerName": "x", "VPCId": "vpc-0other"}]))
    cell._verify_teardown("honuaeksn380570", "honuaeksn380570-it-eks", "vpc-0cell", False)


@pytest.mark.parametrize("failing", ["eks describe-cluster", "ec2 describe-vpcs",
                                     "ec2 describe-network-interfaces", "elb describe-load-balancers"])
def test_an_unreadable_listing_fails_teardown_closed(cell, monkeypatch, failing):
    monkeypatch.setattr(cell, "_run", _aws_world(fail=failing))
    with pytest.raises(ProvisionError, match="could not verify"):
        cell._verify_teardown("honuaeksn380570", "honuaeksn380570-it-eks", "vpc-0cell", False)


def test_teardown_fails_when_the_cells_certificate_or_record_survives(cell, monkeypatch):
    _dns(monkeypatch)
    cell.root("domain_name", "route53_zone_id")
    fqdn = "38057015781-aws-eks-redis-off.cert.demo.honua.io"
    base = _aws_world()

    def run(command, **kwargs):
        if command[1:3] == ["route53", "list-resource-record-sets"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(
                {"ResourceRecordSets": [{"Name": fqdn + ".", "Type": "CNAME"}]}), "")
        if command[1:3] == ["acm", "list-certificates"]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"CertificateSummaryList": []}), "")
        return base(command)

    monkeypatch.setattr(cell, "_run", run)
    with pytest.raises(ProvisionError, match="left " + fqdn.replace(".", r"\.")):
        cell._verify_teardown("honuaeksn380570", "honuaeksn380570-it-eks", None, False)


def test_teardown_deletes_the_cell_record_and_verifies_after_the_destroy(cell, monkeypatch):
    order = []
    monkeypatch.setattr(cell, "_iac_root", lambda: Path("."))
    monkeypatch.setattr(cell, "_run", lambda command, **k: order.append(command[1]) or
                        subprocess.CompletedProcess(command, 1, "", "no cluster"))
    monkeypatch.setattr(cell, "_delete_cell_record", lambda redis: order.append("record"))
    monkeypatch.setattr(cell, "_tf", lambda root, *a, **k: order.append(a[0]) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(cell, "_verify_teardown", lambda *a: order.append("verify"))
    cell.teardown(redis_enabled=False)
    assert order.index("record") < order.index("destroy") < order.index("verify")


def test_a_record_that_cannot_be_deleted_never_blocks_the_destroy(cell, monkeypatch):
    destroyed = []
    monkeypatch.setattr(cell, "_iac_root", lambda: Path("."))
    monkeypatch.setattr(cell, "_run", lambda command, **k: subprocess.CompletedProcess(command, 1, "", ""))

    def refuse(redis):
        raise ProvisionError("could not read the hosted zone records")

    monkeypatch.setattr(cell, "_delete_cell_record", refuse)
    monkeypatch.setattr(cell, "_tf", lambda root, *a, **k: destroyed.append(a[0]) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(cell, "_verify_teardown", lambda *a: None)
    cell.teardown(redis_enabled=True)
    assert destroyed == ["destroy"]


# ---- diagnostics -----------------------------------------------------------------------------------
def test_eks_diagnostics_admit_the_runner_and_redact(cell, monkeypatch):
    granted = []
    monkeypatch.setattr(cell, "grant_operator", lambda **k: granted.append(k))
    cell._kubeconfig = Path("kubeconfig")
    monkeypatch.setattr(cell, "_kubectl", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, f"line\nPassword={cell._db_password}\n", ""))
    report = run_cloud.eks_readiness_diagnostics(cell, redis_enabled=True, redact=run_cloud._redact_log)
    assert granted == [{"redis_enabled": True}]
    assert [s["title"] for s in report["sections"]] == ["pods", "events", "honua log tail", "jobs"]
    assert all(cell._db_password not in line for s in report["sections"] for line in s["lines"])
