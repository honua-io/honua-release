"""Unit tests for the run cost meter (owner decision 9): estimate arithmetic, Cost Explorer states,
and the aggregate's cost rules. No AWS: the CLI and Cost Explorer are stubbed."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import cost_meter  # noqa: E402
import cloud_journey  # noqa: E402

T0 = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)


def _state(*resources, child=()):
    def res(kind, name, **values):
        return {"address": f"{kind}.{name}", "mode": "managed", "type": kind, "name": name, "values": values}
    return {"values": {"root_module": {"resources": [res(*r[:2], **r[2]) for r in resources],
                                       "child_modules": [{"resources": [res(*r[:2], **r[2]) for r in child]}]}}}


ECS_STATE = _state(
    ("aws_lb", "this", {"load_balancer_type": "application", "internal": False, "subnets": ["a", "b"]}),
    ("aws_ecs_task_definition", "this", {"arn": "arn:td:7", "family": "td", "revision": 7, "cpu": "1024",
                                          "memory": "2048", "runtime_platform": [{"cpu_architecture": "X86_64"}]}),
    ("aws_ecs_service", "this", {"desired_count": 1, "task_definition": "arn:td:7", "launch_type": "FARGATE"}),
    ("aws_elasticache_replication_group", "redis", {"node_type": "cache.t3.micro", "num_cache_clusters": 1}),
    ("aws_secretsmanager_secret", "s", {}),
    ("aws_security_group", "sg", {}),
    child=[("aws_db_instance", "this", {"instance_class": "db.t3.micro", "multi_az": False,
                                         "allocated_storage": 20, "publicly_accessible": True}),
           ("aws_nat_gateway", "this", {}), ("aws_eip", "nat", {})])


def _no_aws(args):
    raise AssertionError(f"unexpected AWS call {args}")


def _line(result, price_class):
    return [line for line in result["basis"]["lines"] if line["class"] == price_class]


def test_estimate_prices_every_ecs_resource_class_by_hours():
    inv = cost_meter.inventory(ECS_STATE)
    usage = cost_meter.measure_usage(inv, T0, T0 + timedelta(hours=2), aws=_no_aws)
    result = cost_meter.estimate(inv, usage, started_at=T0, measured_at=T0 + timedelta(hours=2))
    # 2 h + 0.5 h teardown allowance; partial-hour classes bill 3 whole hours.
    assert result["basis"]["hours"] == "2.5000" and result["basis"]["unpriced"] == []
    assert _line(result, "rds:db.t3.micro")[0]["usd"] == "0.0450"            # 2.5 x 0.018
    assert _line(result, "elasticache:cache.t3.micro")[0]["usd"] == "0.0510"  # 3 x 0.017
    assert _line(result, "alb")[0]["usd"] == "0.0675"                         # 3 x 0.0225
    assert _line(result, "nat-gateway")[0]["usd"] == "0.1350"                 # 3 x 0.045
    assert _line(result, "fargate-vcpu:x86_64")[0]["usd"] == "0.1012"         # 1 vCPU x 2.5 x 0.04048
    assert _line(result, "fargate-gb:x86_64")[0]["usd"] == "0.0222"           # 2 GB x 2.5 x 0.004445
    # ALB (2 subnets) + RDS public + NAT EIP = 4 public IPv4 addresses x 3 h x 0.005.
    assert sum(Decimal(l["usd"]) for l in _line(result, "public-ipv4")) == Decimal("0.0600")
    total = sum(Decimal(line["usd"]) for line in result["basis"]["lines"])
    assert abs(total - Decimal(result["estimateUsd"])) < Decimal("0.001")
    assert result["basis"]["pricesAsOf"] == cost_meter.prices()["pricesAsOf"]
    assert any("data transfer" in item for item in result["basis"]["excluded"])


def test_lambda_api_and_batch_usage_is_measured_and_priced():
    state = _state(
        ("aws_lambda_function", "this", {"function_name": "fn", "memory_size": 1024, "architectures": ["x86_64"]}),
        ("aws_apigatewayv2_api", "this", {"id": "api1", "protocol_type": "HTTP"}),
        ("aws_batch_compute_environment", "gp", {"compute_resources": [{"type": "FARGATE_SPOT"}]}),
        ("aws_batch_job_queue", "gp", {"arn": "arn:q"}),
        ("aws_batch_job_definition", "gp", {"arn": "arn:jd:1", "container_properties": json.dumps(
            {"resourceRequirements": [{"type": "VCPU", "value": "4"}, {"type": "MEMORY", "value": "8192"}]})}))
    calls = []

    def aws(args):
        calls.append(args)
        if args[0] == "cloudwatch":
            metric = args[args.index("--metric-name") + 1]
            return {"Datapoints": [{"Sum": {"Duration": 600000.0, "Invocations": 1000.0, "Count": 2000.0}[metric]}]}
        if args[1] == "describe-jobs":
            return {"jobs": [{"jobId": "j", "container": {"resourceRequirements": [
                {"type": "VCPU", "value": "1"}, {"type": "MEMORY", "value": "2048"}]}}]}
        start = int(T0.timestamp() * 1000)
        return {"jobSummaryList": [{"jobId": "j", "jobDefinition": "arn:jd:1", "startedAt": start,
                                    "stoppedAt": start + 3_600_000}]}
    inv = cost_meter.inventory(state)
    usage = cost_meter.measure_usage(inv, T0, T0 + timedelta(hours=1), aws=aws)
    result = cost_meter.estimate(inv, usage, started_at=T0, measured_at=T0 + timedelta(hours=1))
    assert usage["unmeasured"] == []
    assert _line(result, "lambda-gb-second:x86_64")[0]["usd"] == "0.0100"  # 600 GB-s x 0.0000166667
    assert _line(result, "lambda-request")[0]["usd"] == "0.0002"
    assert _line(result, "apigw-http-request")[0]["usd"] == "0.0020"
    vcpu = [l for l in result["basis"]["lines"] if l["class"] == "fargate-vcpu:x86_64"]
    assert vcpu[0]["usd"] == "0.0405" and _line(result, "fargate-gb:x86_64")[0]["usd"] == "0.0089"
    assert any(c[:2] == ["batch", "list-jobs"] for c in calls)


class _Target:
    def _iac_root(self):
        return Path(".")


def test_cell_cost_is_pass_under_fail_over_and_unavailable_when_anything_is_unpriced():
    run = dict(run_id="42", run_attempt="1", aws=_no_aws, clock=lambda: T0 + timedelta(hours=2))
    state = {"startedAt": T0.isoformat()}
    under = cost_meter.cell_cost(_Target(), state, "20", read_state=lambda t: ECS_STATE, **run)
    assert under["status"] == "pass" and under["scope"] == "run" and under["currency"] == "USD"
    assert under["amountUsd"] == under["estimateUsd"] and under["actualUsd"] is None
    assert under["actualAsOf"] is None and under["runId"] == "42" and under["ceilingUsd"] == "20"
    over = cost_meter.cell_cost(_Target(), state, "0.10", read_state=lambda t: ECS_STATE, **run)
    assert over["status"] == "fail"
    odd = _state(("aws_db_instance", "big", {"instance_class": "db.r6g.8xlarge", "allocated_storage": 20}))
    gap = cost_meter.cell_cost(_Target(), state, "20", read_state=lambda t: odd, **run)
    assert gap["status"] == "unavailable" and "db.r6g.8xlarge" in gap["why"]

    def broken(target):
        raise cost_meter.MeterError("terraform show failed: no state")
    missing = cost_meter.cell_cost(_Target(), state, "20", read_state=broken, **run)
    assert missing["status"] == "unavailable" and "no state" in missing["why"]


def test_run_cost_sums_cells_and_names_a_cell_without_an_estimate():
    def cell(name, usd, status="pass"):
        return {"cell": name, "cost": cost_meter.evidence(status=status, run_id="42", run_attempt="1",
                                                          ceiling_usd="20", measured_at="x", estimate_usd=usd,
                                                          basis={})}
    reports = [cell("aws-ecs/redis-off", "1.2500"), cell("aws-ecs/redis-on", "2.0000"),
               {"cell": "aws-eks/redis-off"}]  # never provisioned: nothing billed
    run = cost_meter.run_cost(reports, "20", run_id="42", run_attempt="1")
    assert run["status"] == "pass" and run["estimateUsd"] == "3.2500" and run["amount"] == "3.2500"
    assert run["estimateBasis"]["cells"] == {"aws-ecs/redis-off": "1.2500", "aws-ecs/redis-on": "2.0000"}
    assert cost_meter.run_cost(reports, "3", run_id="42", run_attempt="1")["status"] == "fail"
    reports.append(cell("aws-serverless/redis-off", None, "unavailable"))
    gap = cost_meter.run_cost(reports, "20", run_id="42", run_attempt="1")
    assert gap["status"] == "unavailable" and "aws-serverless/redis-off" in gap["why"]


def test_run_tags_carry_run_cell_and_ceiling():
    assert cost_meter.run_tags("123", "aws-ecs/redis-on", "20") == {
        "honua-release:run-id": "123", "honua-release:cell": "aws-ecs/redis-on",
        "honua-release:cost-ceiling-usd": "20"}


class _StubCE:
    def __init__(self, status, rows=()):
        self.status, self.rows, self.queries = status, list(rows), []

    def tag_status(self, key):
        return self.status

    def daily_usage_cost(self, start, end, keys):
        self.queries.append((start, end, keys))
        return self.rows


def _row(day, *groups):
    return {"TimePeriod": {"Start": day}, "Groups": [
        {"Keys": [f"honua-release:run-id${run}", f"honua-release:cost-ceiling-usd${ceiling}"],
         "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}} for run, ceiling, amount in groups]}


def test_cost_explorer_reports_tag_not_activated_honestly():
    for status in (None, "Inactive"):
        result = cost_meter.prior_run_costs(_StubCE(status), exclude_run="9", default_ceiling_usd="20",
                                            clock=lambda: T0)
        assert result["status"] == "tag-not-activated" and result["runs"] == []
        assert "Cost allocation tags" in result["why"]


def test_cost_explorer_marks_recent_runs_pending_and_settled_runs_measured():
    rows = [_row("2026-10-08", ("100", "20", "3.10"), ("", "", "50"), ("9", "20", "4")),
            _row("2026-10-09", ("100", "20", "0.40"), ("101", "", "1.00")),
            _row("2026-10-10", ("101", "", "0.5"))]
    ce = _StubCE("Active", rows)
    result = cost_meter.prior_run_costs(ce, exclude_run="9", default_ceiling_usd="25",
                                        clock=lambda: T0)  # 2026-10-10 08:00Z
    assert ce.queries[0][2] == [cost_meter.RUN_ID_TAG, cost_meter.CEILING_TAG]
    runs = {r["runId"]: r for r in result["runs"]}
    assert set(runs) == {"100", "101"}  # the current run and untagged spend are excluded
    # Last billed day 10-09 settles at 10-11 00:00Z: still pending at 10-10 08:00Z.
    assert runs["100"]["status"] == "pending" and runs["100"]["actualUsd"] == "3.5000"
    assert runs["101"]["ceilingUsd"] == "25" and runs["101"]["ceilingSource"] == "current-run-input"
    later = cost_meter.prior_run_costs(ce, exclude_run="9", default_ceiling_usd="25",
                                       clock=lambda: T0 + timedelta(days=1, hours=12))
    settled = {r["runId"]: r for r in later["runs"]}
    assert settled["100"]["status"] == "measured" and settled["100"]["ceilingSource"] == "run-tag"
    assert settled["101"]["status"] == "pending"  # billed on 10-10 too


def test_cost_explorer_failure_is_unavailable_not_zero():
    class Broken(_StubCE):
        def daily_usage_cost(self, *a):
            raise cost_meter.MeterError("AccessDenied")
    result = cost_meter.prior_run_costs(Broken("Active"), exclude_run="9", default_ceiling_usd="20", clock=lambda: T0)
    assert result["status"] == "unavailable" and "AccessDenied" in result["why"]


def _prior(status, actual, ceiling="20", run="100"):
    return {"status": "measured", "runs": [{"status": status, "runId": run, "actualUsd": actual,
                                             "ceilingUsd": ceiling, "actualAsOf": "2026-10-09"}]}


def test_prior_run_actual_over_its_ceiling_fails_the_next_certifying_run_naming_it():
    failures = cloud_journey.prior_run_failures(_prior("measured", "21.50"))
    assert failures and "prior run 100" in failures[0] and "$20 ceiling" in failures[0]
    assert cloud_journey.prior_run_failures(_prior("measured", "19.99")) == []
    assert cloud_journey.prior_run_failures(_prior("pending", "99")) == []
    assert cloud_journey.prior_run_failures({"status": "tag-not-activated", "runs": []}) == []
    report = cloud_journey.aggregate([], E2E_DIR, require_real=True, full_scope=True, run_id="200",
                                     run_attempt="1", final_cost={"status": "pass"},
                                     prior_runs=_prior("measured", "21.50"))
    assert report["status"] == "fail" and "prior run 100" in report["why"]
    assert report["priorRunCosts"]["runs"][0]["runId"] == "100"
    focused = cloud_journey.aggregate([], E2E_DIR, require_real=False, full_scope=False, run_id="200",
                                      run_attempt="1", prior_runs=_prior("measured", "21.50"))
    assert "prior run 100" not in focused["why"]


def test_aggregate_accepts_the_estimate_as_the_run_cost_under_and_fails_it_over(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "300")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    reports = [{"cell": "aws-ecs/redis-off", "cost": cost_meter.evidence(
        status="pass", run_id="300", run_attempt="1", ceiling_usd="20", measured_at="x",
        estimate_usd="4.0000", basis={})}]
    meter = tmp_path / "run-cost.json"
    meter.write_text(json.dumps(cost_meter.run_cost(reports, "20", run_id="300", run_attempt="1")))
    final = cloud_journey.check_cost(meter, "20", started_at=started)
    assert final["status"] == "pass" and final["amountUsd"] == "4.0000" and final["estimateUsd"] == "4.0000"
    assert final["meter"] == "estimate" and final["actualUsd"] is None
    over = cloud_journey.check_cost(meter, "3", started_at=started)
    assert over["status"] == "fail"
    report = cloud_journey.aggregate([], tmp_path, require_real=True, full_scope=True, run_id="300",
                                     run_attempt="1", final_cost=over)
    assert report["status"] == "fail" and "final run cost" in report["why"]
    reports[0]["cost"]["status"], reports[0]["cost"]["estimateUsd"] = "unavailable", None
    meter.write_text(json.dumps(cost_meter.run_cost(reports, "20", run_id="300", run_attempt="1")))
    gap = cloud_journey.check_cost(meter, "20", started_at=started)
    assert gap["status"] == "unavailable" and "aws-ecs/redis-off" in gap["why"]


def test_eks_estimate_prices_the_helm_load_balancer_outside_state():
    inv = cost_meter.inventory(_state(("aws_eks_cluster", "this", {}), ("aws_eks_node_group", "n", {
        "scaling_config": [{"desired_size": 2}], "instance_types": ["t3.medium"], "disk_size": 20})))
    result = cost_meter.estimate(inv, {"usage": [], "unmeasured": []}, started_at=T0,
                                 measured_at=T0 + timedelta(hours=1))
    assert result["basis"]["unpriced"] == []
    assert _line(result, "elb-classic") and _line(result, "eks-cluster")[0]["usd"] == "0.2000"  # 2 h ceil
    assert _line(result, "ec2:t3.medium")[0]["usd"] == "0.1248"  # 2 nodes x 1.5 h x 0.0416


def test_cells_pass_the_run_tags_only_to_a_root_that_declares_them(tmp_path, monkeypatch):
    from targets import terraform_target
    root = tmp_path / terraform_target.ECS_SPEC.root
    root.mkdir(parents=True)
    monkeypatch.setenv("HONUA_IAC_DIR", str(tmp_path))
    monkeypatch.setenv("HONUA_CLOUD_COST_CEILING_USD", "20")
    target = terraform_target.ecs(run_id="987654321")
    assert target._tag_vars(True) == []
    (root / "variables.tf").write_text('variable "tags" {\n  type = map(string)\n}\n')
    (flag,) = target._tag_vars(True)
    assert flag.startswith("-var=tags=") and json.loads(flag.split("=", 2)[2]) == {
        "honua-release:run-id": "987654321", "honua-release:cell": "aws-ecs/redis-on",
        "honua-release:cost-ceiling-usd": "20"}
