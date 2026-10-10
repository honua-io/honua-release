"""Run-scoped AWS cost evidence for the cloud cells (owner decision 9, 2026-10-10).

Two readings, recorded side by side and never confused with each other:

* **estimate** (live, this run): taken at teardown, before ``terraform destroy``, from the cell's own
  Terraform state (what was deployed and its size), the hours since provision began, and the
  measured request/duration usage of its Lambda functions, HTTP APIs and Batch jobs. Each resource
  class is priced from ``cost-prices.json``, a committed table of public on-demand list prices with a
  ``pricesAsOf`` date. Whatever the table does not price is named in the basis, never guessed.
* **actual** (lagged, earlier runs): AWS Cost Explorer ``GetCostAndUsage``, UNBLENDED usage cost,
  daily, filtered by the ``honua-release:run-id`` cost-allocation tag every cell resource carries.
  Cost Explorer lags by about a day, so the actual for a run is read by a LATER run: the ceiling is
  enforceable at the next run, not during this one. Until Cost Explorer has caught up the reading is
  ``pending``; while the tag is not activated as a cost-allocation tag it is ``tag-not-activated``.

Credits are excluded on purpose: this account carries promotional credits, so plain UnblendedCost
nets to ~0 and would pass every ceiling. The query filters ``RECORD_TYPE=Usage``.

    python e2e/cost_meter.py run --reports reports --output e2e/cloud-evidence/run-cost.json
    python e2e/cost_meter.py prior-runs --output e2e/cloud-evidence/prior-run-costs.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

RUN_ID_TAG = "honua-release:run-id"
CELL_TAG = "honua-release:cell"
CEILING_TAG = "honua-release:cost-ceiling-usd"
PRICES_PATH = Path(__file__).with_name("cost-prices.json")
# terraform destroy is not instant (RDS deletion alone takes minutes) and everything bills until gone.
TEARDOWN_ALLOWANCE_HOURS = Decimal("0.5")
# Cost Explorer finishes a UTC day roughly a day after it ends; until then a run's figure is pending.
CE_SETTLE_HOURS = 24
# Lambda/API metrics are published a minute or more after the invocation; an empty reading at
# teardown is re-read once after this wait before it is accepted as zero (and named as such).
METRIC_PUBLICATION_WAIT_SECONDS = 120
# How far back the full billed history of a run is read; a run billed before this is incomplete.
CE_HISTORY_DAYS = 90
# Credits and refunds are not spend; every other record type (on-demand, commitment-covered) is.
CE_SPEND_FILTER = {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}}
CENT = Decimal("0.0001")


class MeterError(RuntimeError):
    pass


def prices(path: Path = PRICES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _usd(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def run_tags(run_id: str, cell: str, ceiling_usd: str | None = None) -> dict:
    """The tags every cell resource carries, through the IaC root's ``tags`` variable."""
    tags = {RUN_ID_TAG: str(run_id), CELL_TAG: cell}
    if ceiling_usd:
        tags[CEILING_TAG] = format(Decimal(str(ceiling_usd)).normalize(), "f")
    return tags


def _aws_cli(args: list[str]) -> dict:
    proc = subprocess.run(["aws", *args, "--output", "json"], text=True, capture_output=True,
                          check=False, timeout=180)
    if proc.returncode:
        raise MeterError(f"aws {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return json.loads(proc.stdout or "{}")


# ---- live estimate --------------------------------------------------------------------------------

def _resources(module: dict):
    for resource in module.get("resources", []):
        if resource.get("mode", "managed") == "managed":
            yield resource
    for child in module.get("child_modules", []):
        yield from _resources(child)


def _first(value):
    return value[0] if isinstance(value, list) and value else (value if isinstance(value, dict) else {})


def inventory(state: dict) -> dict:
    """Billable resources from ``terraform show -json``. Resource types not listed bill nothing
    themselves (security groups, IAM, subnets, routes, log groups are named in ``excluded``)."""
    resources = list(_resources(state.get("values", {}).get("root_module", {})))
    hourly, monthly, lambdas, apis, queues, unpriced = [], [], [], [], [], []
    task_defs = [r["values"] for r in resources if r["type"] == "aws_ecs_task_definition"]
    job_defs = {}
    for r in resources:
        if r["type"] == "aws_batch_job_definition":
            v = r["values"]
            spec = v.get("container_properties") or "{}"
            spec = json.loads(spec) if isinstance(spec, str) else spec
            need = {req.get("type"): req.get("value") for req in spec.get("resourceRequirements", [])}
            arch = _first(spec.get("runtimePlatform") or {}).get("cpuArchitecture", "X86_64")
            entry = {"vcpu": need.get("VCPU"), "memoryMiB": need.get("MEMORY"),
                     "architecture": "arm64" if str(arch).upper() == "ARM64" else "x86_64"}
            for key in (v.get("arn"), v.get("arn_prefix"), v.get("name")):
                if key:
                    job_defs[key] = entry
    for r in resources:
        v, address, kind = r.get("values") or {}, r.get("address", r["type"]), r["type"]
        if kind == "aws_db_instance":
            hourly.append((address, f"rds:{v.get('instance_class')}", 2 if v.get("multi_az") else 1))
            monthly.append((address, "rds-storage-gb", int(v.get("allocated_storage") or 0)))
            if v.get("publicly_accessible"):
                hourly.append((address, "public-ipv4", 1))
        elif kind == "aws_elasticache_replication_group":
            nodes = len(v.get("member_clusters") or []) or int(v.get("num_cache_clusters") or 1)
            hourly.append((address, f"elasticache:{v.get('node_type')}", nodes))
        elif kind == "aws_elasticache_cluster":
            hourly.append((address, f"elasticache:{v.get('node_type')}", int(v.get("num_cache_nodes") or 1)))
        elif kind == "aws_lb":
            if v.get("load_balancer_type", "application") != "application":
                unpriced.append(f"{address} ({v.get('load_balancer_type')} load balancer)")
                continue
            hourly.append((address, "alb", 1))
            hourly.append((address, "alb-lcu", 1))
            if not v.get("internal"):
                hourly.append((address, "public-ipv4", len(v.get("subnets") or []) or 2))
        elif kind == "aws_nat_gateway":
            hourly.append((address, "nat-gateway", 1))
        elif kind == "aws_eip":
            hourly.append((address, "public-ipv4", 1))
        elif kind == "aws_eks_cluster":
            hourly.append((address, "eks-cluster", 1))
            # The EKS cell's endpoint is a Kubernetes Service of type LoadBalancer that Helm creates
            # outside Terraform state (a Classic ELB without the LB controller); priced explicitly.
            service_lb = f"{address} -> Service/LoadBalancer (Helm, outside Terraform state)"
            hourly.append((service_lb, "elb-classic", 1))
            hourly.append((service_lb, "public-ipv4", 2))
        elif kind == "aws_eks_node_group":
            scaling = _first(v.get("scaling_config"))
            count = int(scaling.get("desired_size") or 0)
            for instance_type in (v.get("instance_types") or ["t3.medium"])[:1]:
                hourly.append((address, f"ec2:{instance_type}", count))
            monthly.append((address, "ebs-gp3-gb", count * int(v.get("disk_size") or 20)))
        elif kind == "aws_instance":
            hourly.append((address, f"ec2:{v.get('instance_type')}", 1))
        elif kind == "aws_ecs_service":
            count = int(v.get("desired_count") or 0)
            if count == 0 or v.get("launch_type") == "EC2":
                continue  # EC2-launched tasks bill through their instances, priced above
            ref = str(v.get("task_definition") or "")
            match = next((t for t in task_defs if ref and ref in (
                t.get("arn"), t.get("arn_without_revision"), t.get("family"),
                f"{t.get('family')}:{t.get('revision')}")), None)
            if match is None:
                unpriced.append(f"{address} (task definition {ref or 'unknown'} not in state)")
                continue
            arch = str(_first(match.get("runtime_platform")).get("cpu_architecture") or "X86_64")
            arch = "arm64" if arch.upper() == "ARM64" else "x86_64"
            vcpu = Decimal(str(match.get("cpu") or 0)) / 1024
            gb = Decimal(str(match.get("memory") or 0)) / 1024
            hourly.append((address, f"fargate-vcpu:{arch}", vcpu * count))
            hourly.append((address, f"fargate-gb:{arch}", gb * count))
            if _first(v.get("network_configuration")).get("assign_public_ip"):
                hourly.append((address, "public-ipv4", count))
        elif kind == "aws_secretsmanager_secret":
            monthly.append((address, "secretsmanager-secret", 1))
        elif kind == "aws_kms_key":
            monthly.append((address, "kms-key", 1))
        elif kind == "aws_lambda_function":
            arch = (v.get("architectures") or ["x86_64"])[0]
            lambdas.append({"address": address, "name": v.get("function_name"),
                            "memoryMb": int(v.get("memory_size") or 128),
                            "architecture": "arm64" if arch == "arm64" else "x86_64"})
        elif kind == "aws_apigatewayv2_api":
            if v.get("protocol_type", "HTTP") == "HTTP":
                apis.append({"address": address, "id": v.get("id")})
            else:
                unpriced.append(f"{address} ({v.get('protocol_type')} API)")
        elif kind == "aws_batch_compute_environment":
            kind_ = _first(v.get("compute_resources")).get("type", "")
            if kind_ not in ("FARGATE", "FARGATE_SPOT"):
                unpriced.append(f"{address} ({kind_ or 'unmanaged'} Batch compute environment)")
        elif kind == "aws_batch_job_queue":
            queues.append({"address": address, "arn": v.get("arn") or v.get("name")})
    return {"hourly": hourly, "monthly": monthly, "lambdas": lambdas, "apis": apis,
            "batchQueues": queues, "batchJobDefinitions": job_defs, "unpriced": unpriced}


def _metric_sum(aws, namespace: str, metric: str, dimension: tuple[str, str],
                start: datetime, end: datetime) -> Decimal | None:
    """The metric's Sum over [start, end], or None when CloudWatch returned no datapoints at all."""
    seconds = max(60, (end - start).total_seconds())
    period = max(60, math.ceil(seconds / 1440 / 60) * 60)
    body = aws(["cloudwatch", "get-metric-statistics", "--namespace", namespace, "--metric-name", metric,
                "--dimensions", f"Name={dimension[0]},Value={dimension[1]}",
                "--start-time", start.isoformat(), "--end-time", end.isoformat(),
                "--period", str(period), "--statistics", "Sum"])
    points = body.get("Datapoints", [])
    if not points:
        return None
    return sum((Decimal(str(point.get("Sum", 0))) for point in points), Decimal(0))


def measure_usage(inv: dict, start: datetime, end: datetime, aws=_aws_cli, *, sleep=time.sleep,
                  publication_wait: int = METRIC_PUBLICATION_WAIT_SECONDS) -> dict:
    """Request/duration-billed usage over [start, end]. A reading that fails is named, not zeroed."""
    usage, unmeasured, no_datapoints = [], [], []
    # (address, namespace, metric, dimension, price class, scale(sum) -> billed units)
    metrics = []
    for fn in inv["lambdas"]:
        dim = ("FunctionName", fn["name"])
        gb = Decimal(fn["memoryMb"]) / 1024
        metrics.append((fn["address"], "AWS/Lambda", "Duration", dim, f"lambda-gb-second:{fn['architecture']}",
                        lambda ms, gb=gb: ms / 1000 * gb))
        metrics.append((fn["address"], "AWS/Lambda", "Invocations", dim, "lambda-request", lambda n: n))
    for api in inv["apis"]:
        metrics.append((api["address"], "AWS/ApiGateway", "Count", ("ApiId", api["id"]),
                        "apigw-http-request", lambda n: n))
    empty = []
    for spec in metrics:
        address, namespace, metric, dim, price_class, scale = spec
        try:
            total = _metric_sum(aws, namespace, metric, dim, start, end)
        except Exception as error:
            unmeasured.append(f"{address} {metric}: {type(error).__name__}: {error}")
            continue
        if total is None:
            empty.append(spec)
        else:
            usage.append((address, price_class, scale(total)))
    if empty and publication_wait > 0:
        # CloudWatch may not have published the last minute(s) yet: read the empty ones once more.
        sleep(publication_wait)
        late = end + timedelta(seconds=publication_wait)
        for address, namespace, metric, dim, price_class, scale in empty:
            try:
                total = _metric_sum(aws, namespace, metric, dim, start, late)
            except Exception as error:
                unmeasured.append(f"{address} {metric}: {type(error).__name__}: {error}")
                continue
            if total is None:
                no_datapoints.append(f"{address} {metric}")
            else:
                usage.append((address, price_class, scale(total)))
    else:
        no_datapoints.extend(f"{spec[0]} {spec[2]}" for spec in empty)
    since = str(int(start.timestamp() * 1000))
    for queue in inv["batchQueues"]:
        try:
            body = aws(["batch", "list-jobs", "--job-queue", queue["arn"],
                        "--filters", f"name=AFTER_CREATED_AT,values={since}"])
        except Exception as error:
            unmeasured.append(f"{queue['address']} Batch jobs: {type(error).__name__}: {error}")
            continue
        started_jobs = [job for job in body.get("jobSummaryList", []) if job.get("startedAt")]
        # The server overrides vCPU/memory per job at SubmitJob, so the job's own requirements win
        # over the job definition's defaults whenever DescribeJobs returns them.
        sized, unsized = {}, set()
        for offset in range(0, len(started_jobs), 100):
            ids = [job["jobId"] for job in started_jobs[offset:offset + 100]]
            try:
                described = aws(["batch", "describe-jobs", "--jobs", *ids])
            except Exception as error:
                # Without the job's own (overridden) size the job definition default could underprice.
                unmeasured.append(f"{queue['address']} Batch job sizing for {len(ids)} job(s): "
                                  f"{type(error).__name__}: {error}")
                unsized.update(ids)
                continue
            for detail in described.get("jobs", []):
                need = {req.get("type"): req.get("value")
                        for req in (detail.get("container") or {}).get("resourceRequirements", [])}
                if need.get("VCPU") and need.get("MEMORY"):
                    sized[detail.get("jobId")] = need
        for job in [job for job in started_jobs if job["jobId"] not in unsized]:
            definition = inv["batchJobDefinitions"].get(job.get("jobDefinition", ""))
            if job.get("jobId") in sized and definition is not None:
                definition = {**definition, "vcpu": sized[job["jobId"]]["VCPU"],
                              "memoryMiB": sized[job["jobId"]]["MEMORY"]}
            if definition is None or not definition["vcpu"] or not definition["memoryMiB"]:
                unmeasured.append(f"{queue['address']} job {job.get('jobId')}: job definition "
                                  f"{job.get('jobDefinition')} not sized in state")
                continue
            stopped = job.get("stoppedAt") or int(end.timestamp() * 1000)
            hours = max(Decimal(60), Decimal(stopped - job["startedAt"]) / 1000) / 3600  # 1-minute minimum
            arch = definition["architecture"]
            usage.append((queue["address"], f"fargate-vcpu:{arch}", Decimal(definition["vcpu"]) * hours))
            usage.append((queue["address"], f"fargate-gb:{arch}",
                          Decimal(definition["memoryMiB"]) / 1024 * hours))
    return {"usage": usage, "unmeasured": unmeasured, "noDatapoints": no_datapoints}


def estimate(inv: dict, usage: dict, *, started_at: datetime, measured_at: datetime,
             table: dict | None = None) -> dict:
    table = table or prices()
    hours = Decimal(str((measured_at - started_at).total_seconds())) / 3600 + TEARDOWN_ALLOWANCE_HOURS
    hours = max(hours, Decimal(0))
    month = Decimal(table["hoursPerMonth"])
    ceil_classes = set(table["partialHourBilledAsFull"])
    lines, unpriced = [], list(inv["unpriced"])
    total = Decimal(0)

    def add(address, price_class, quantity, units, unit, unit_price):
        nonlocal total
        usd = Decimal(quantity) * units * Decimal(unit_price)
        total += usd
        lines.append({"resource": address, "class": price_class, "quantity": str(quantity),
                      "unit": unit, "units": str(units.quantize(CENT)), "unitPriceUsd": unit_price,
                      "usd": _usd(usd)})

    for address, price_class, quantity in inv["hourly"]:
        price = table["hourly"].get(price_class)
        if price is None:
            unpriced.append(f"{address} ({price_class})")
            continue
        base = price_class.split(":", 1)[0]
        billed = Decimal(math.ceil(hours)) if base in ceil_classes else hours
        add(address, price_class, quantity, billed, "hour", price)
    for address, price_class, quantity in inv["monthly"]:
        price = table["monthly"].get(price_class)
        if price is None:
            unpriced.append(f"{address} ({price_class})")
            continue
        add(address, price_class, quantity, hours / month, "month", price)
    for address, price_class, amount in usage["usage"]:
        price = table["usage"].get(price_class) or table["hourly"].get(price_class)
        if price is None:
            unpriced.append(f"{address} ({price_class})")
            continue
        add(address, price_class, 1, Decimal(amount), "measured", price)
    return {"estimateUsd": _usd(total),
            "basis": {"method": "terraform-state inventory x hours since provision + measured usage, "
                                "priced from e2e/cost-prices.json",
                      "pricesAsOf": table["pricesAsOf"], "priceSource": table["source"],
                      "region": table["region"], "billedFrom": started_at.isoformat(),
                      "billedTo": measured_at.isoformat(), "hours": str(hours.quantize(CENT)),
                      "teardownAllowanceHours": str(TEARDOWN_ALLOWANCE_HOURS),
                      "lines": lines, "unpriced": unpriced, "unmeasured": list(usage["unmeasured"]),
                      "noDatapointsAfterRetry": list(usage.get("noDatapoints", [])),
                      "excluded": table["excluded"]}}


def terraform_state(target) -> dict:
    root = getattr(target, "_workdir", None) or target._iac_root()
    if root is None:
        raise MeterError("no Terraform root for this cell")
    proc = subprocess.run(["terraform", f"-chdir={root}", "show", "-json"], text=True,
                          capture_output=True, check=False, timeout=300)
    if proc.returncode:
        raise MeterError(f"terraform show failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return json.loads(proc.stdout or "{}")


def meters(target) -> bool:
    """Whether the live estimate can read this target's deployed inventory."""
    return callable(getattr(target, "_iac_root", None))


def evidence(*, status: str, run_id: str, run_attempt: str, ceiling_usd, measured_at: str,
             estimate_usd: str | None, basis: dict | None, why: str | None = None, **extra) -> dict:
    """The cost evidence object. ``amountUsd`` repeats the figure the gate compares (the estimate for
    the current run) under the name the existing consumers already read."""
    body = {"status": status, "scope": "run", "runId": run_id, "runAttempt": run_attempt,
            "currency": "USD", "meter": "estimate", "estimateUsd": estimate_usd, "estimateBasis": basis,
            "actualUsd": None, "actualAsOf": None, "ceilingUsd": str(ceiling_usd), "measuredAt": measured_at}
    if estimate_usd is not None:
        body["amountUsd"] = estimate_usd
    if why:
        body["why"] = why
    body.update(extra)
    return body


def cell_cost(target, state: dict, ceiling_usd, *, run_id: str, run_attempt: str,
              read_state=terraform_state, aws=_aws_cli, clock=None, usage_options=None) -> dict:
    """The cell's live estimate, read before destroy. ``unavailable`` names what could not be priced."""
    measured = clock() if clock else datetime.now(timezone.utc)
    measured_at = measured.isoformat().replace("+00:00", "Z")
    ceiling = Decimal(str(ceiling_usd))
    try:
        started = _parse_time(state["startedAt"])
        deployed = read_state(target)
        if not deployed.get("values"):
            raise MeterError("no Terraform state to price (the cell provisioned, but its state is absent)")
        inv = inventory(deployed)
        result = estimate(inv, measure_usage(inv, started, measured, aws=aws, **(usage_options or {})),
                          started_at=started, measured_at=measured)
    except Exception as error:
        return evidence(status="unavailable", run_id=run_id, run_attempt=run_attempt, ceiling_usd=ceiling,
                        measured_at=measured_at, estimate_usd=None, basis=None,
                        why=f"live cost estimate unavailable: {type(error).__name__}: {error}")
    basis = result["basis"]
    gaps = basis["unpriced"] + basis["unmeasured"]
    if gaps:
        return evidence(status="unavailable", run_id=run_id, run_attempt=run_attempt, ceiling_usd=ceiling,
                        measured_at=measured_at, estimate_usd=result["estimateUsd"], basis=basis,
                        why="live cost estimate incomplete: " + "; ".join(gaps))
    status = "fail" if Decimal(result["estimateUsd"]) > ceiling else "pass"
    return evidence(status=status, run_id=run_id, run_attempt=run_attempt, ceiling_usd=ceiling,
                    measured_at=measured_at, estimate_usd=result["estimateUsd"], basis=basis)


def run_cost(reports: list[dict], ceiling_usd, *, run_id: str, run_attempt: str,
             expected_cells: list[str] | None = None) -> dict:
    """The run's estimate: the sum of every provisioned cell's estimate in this run attempt. Every
    expected (dispatched) cell, Preview included, must report: either its estimate or that it never
    provisioned anything."""
    ceiling = Decimal(str(ceiling_usd))
    cells, missing, total = {}, [], Decimal(0)
    reported = {report.get("cell") for report in reports}
    missing.extend(f"{cell} (no cell report)" for cell in expected_cells or [] if cell not in reported)
    for report in reports:
        cost = report.get("cost")
        cell = report.get("cell") or report.get("gate", "unknown")
        if not isinstance(cost, dict):
            if report.get("cell") and report.get("provisionAttempted") is not False:
                missing.append(f"{cell} (no cost evidence and no record that provisioning never ran)")
            continue  # never provisioned (or not a cell report): nothing deployed, nothing billed
        if (cost.get("runId"), cost.get("runAttempt")) != (run_id, run_attempt):
            missing.append(f"{cell} (cost bound to another run)")
            continue
        if cost.get("estimateUsd") is None or cost.get("status") == "unavailable":
            missing.append(f"{cell} ({cost.get('why', 'no estimate')})")
            continue
        cells[cell] = cost["estimateUsd"]
        total += Decimal(cost["estimateUsd"])
    basis = {"method": "sum of per-cell live estimates (e2e/cost_meter.py cell_cost)", "cells": cells,
             "notCovered": ["the iac-live job's separate honua-iac workflow run (its own deployment, "
                            "lifecycle and teardown; not tagged with this run id)",
                            "cells of earlier attempts of this run (aggregate adds their settled Cost "
                            "Explorer actual before a rerun certifies)"]}
    measured_at = now()
    if missing:
        return evidence(status="unavailable", run_id=run_id, run_attempt=run_attempt, ceiling_usd=ceiling,
                        measured_at=measured_at, estimate_usd=_usd(total) if cells else None, basis=basis,
                        why="run cost estimate incomplete: " + "; ".join(missing))
    status = "fail" if total > ceiling else "pass"
    body = evidence(status=status, run_id=run_id, run_attempt=run_attempt, ceiling_usd=ceiling,
                    measured_at=measured_at, estimate_usd=_usd(total), basis=basis)
    body["amount"] = body["amountUsd"]  # the field check_cost reads from run-cost.json
    return body


# ---- lagged actuals: Cost Explorer ----------------------------------------------------------------

class CostExplorer:
    """``ce:ListCostAllocationTags`` + ``ce:GetCostAndUsage`` through the aws CLI (us-east-1)."""

    def __init__(self, aws=_aws_cli):
        self._aws = aws

    def tag_status(self, key: str) -> str | None:
        body = self._aws(["ce", "list-cost-allocation-tags", "--tag-keys", key, "--region", "us-east-1"])
        return next((t.get("Status") for t in body.get("CostAllocationTags", []) if t.get("TagKey") == key), None)

    def daily_usage_cost(self, start: date, end: date, group_keys: list[str],
                         run_id: str | None = None) -> list[dict]:
        spend = CE_SPEND_FILTER
        if run_id is not None:
            spend = {"And": [CE_SPEND_FILTER, {"Tags": {"Key": RUN_ID_TAG, "Values": [str(run_id)],
                                                       "MatchOptions": ["EQUALS"]}}]}
        args = ["ce", "get-cost-and-usage", "--region", "us-east-1",
                "--time-period", f"Start={start.isoformat()},End={end.isoformat()}",
                "--granularity", "DAILY", "--metrics", "UnblendedCost", "--filter", json.dumps(spend)]
        if group_keys:
            args += ["--group-by", *[f"Type=TAG,Key={key}" for key in group_keys]]
        results, token = [], None
        while True:
            body = self._aws(args + (["--next-page-token", token] if token else []))
            results.extend(body.get("ResultsByTime", []))
            token = body.get("NextPageToken")
            if not token:
                return results


def _tag_value(key: str) -> str:
    return key.split("$", 1)[1] if "$" in key else ""


def _settled_at(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc) \
        + timedelta(hours=CE_SETTLE_HOURS)


def prior_run_costs(ce: CostExplorer, *, current_run: str, default_ceiling_usd, lookback_days: int = 3,
                    clock=None) -> dict:
    """Cost Explorer actuals for the runs whose tagged resources billed in the lookback window.

    Each run found is then read over its whole billed history (CE_HISTORY_DAYS), so a run older than
    the window, or an orphan still accruing, is judged on everything it has billed. ``settledUsd``
    counts only days Cost Explorer has had a day to finish; ``actualUsd`` counts every day seen. A
    run is ``measured`` once all its billed days are settled, ``pending`` before, ``incomplete``
    when its history reaches past CE_HISTORY_DAYS. The current run's own row (earlier attempts of a
    rerun) is marked ``currentRun`` and judged by aggregate, not here."""
    current = clock() if clock else datetime.now(timezone.utc)
    measured_at = current.isoformat().replace("+00:00", "Z")
    head = {"scope": "prior-runs", "currency": "USD", "measuredAt": measured_at, "tag": RUN_ID_TAG,
            "lookbackDays": lookback_days, "historyDays": CE_HISTORY_DAYS, "currentRunId": str(current_run),
            "recordTypes": "all but Credit and Refund", "runs": []}
    try:
        status = ce.tag_status(RUN_ID_TAG)
    except Exception as error:
        return {**head, "status": "unavailable", "why": f"ce:ListCostAllocationTags failed: {error}"}
    if status != "Active":
        return {**head, "status": "tag-not-activated",
                "why": f"{RUN_ID_TAG} is {'not yet seen by Billing' if status is None else status}; the "
                       "owner activates it once under Billing > Cost allocation tags (e2e/README.md)"}
    end = current.date() + timedelta(days=1)
    try:
        rows = ce.daily_usage_cost(current.date() - timedelta(days=lookback_days), end, [RUN_ID_TAG, CEILING_TAG])
    except Exception as error:
        return {**head, "status": "unavailable", "why": f"ce:GetCostAndUsage failed: {error}"}
    found: dict[str, set] = {}
    for row in rows:
        for group in row.get("Groups", []):
            keys = group.get("Keys", [])
            run_id = _tag_value(keys[0]) if keys else ""
            if run_id and Decimal(str(group["Metrics"]["UnblendedCost"]["Amount"])) > 0:
                ceilings = found.setdefault(run_id, set())
                if len(keys) > 1 and _tag_value(keys[1]):
                    ceilings.add(_tag_value(keys[1]))
    history_start = current.date() - timedelta(days=CE_HISTORY_DAYS)
    out = []
    for run_id in sorted(found):
        try:
            history = ce.daily_usage_cost(history_start, end, [], run_id=run_id)
        except Exception as error:
            return {**head, "status": "unavailable", "why": f"ce:GetCostAndUsage failed for run {run_id}: {error}"}
        days, estimated = {}, False
        for row in history:
            amount = Decimal(str((row.get("Total", {}).get("UnblendedCost") or {}).get("Amount", "0")))
            if amount > 0:
                day = date.fromisoformat(row["TimePeriod"]["Start"])
                days[day] = days.get(day, Decimal(0)) + amount
                estimated = estimated or bool(row.get("Estimated"))
        if not days:
            continue
        actual = sum(days.values(), Decimal(0))
        settled = sum((amount for day, amount in days.items() if current >= _settled_at(day)), Decimal(0))
        ceilings = sorted(found[run_id], key=Decimal)
        ceiling, source = ((ceilings[0], "run-tag") if ceilings else (str(default_ceiling_usd), "current-run-input"))
        truncated = min(days) <= history_start
        status = ("incomplete" if truncated else
                  "measured" if all(current >= _settled_at(day) for day in days) else "pending")
        out.append({"status": status, "scope": "run", "runId": run_id, "currentRun": run_id == str(current_run),
                    "currency": "USD", "estimateUsd": None, "estimateBasis": None,
                    "actualUsd": _usd(actual), "settledUsd": _usd(settled),
                    "actualAsOf": (max(days) + timedelta(days=1)).isoformat(),
                    "ceilingUsd": ceiling, "ceilingSource": source, "measuredAt": measured_at,
                    "firstBilledDay": min(days).isoformat(), "lastBilledDay": max(days).isoformat(),
                    # Cost Explorer flags every day of the open billing month as Estimated until the
                    # invoice closes; recorded, not waited on (that would defer enforcement a month).
                    "estimatedByCostExplorer": estimated,
                    "overCeiling": settled > Decimal(ceiling)})
    return {**head, "status": "measured", "runs": out}


# ---- CLI ------------------------------------------------------------------------------------------

def _load_reports(root: Path) -> list[dict]:
    reports = []
    for path in sorted(root.rglob("gate-report-cloud.json")):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="sum the cells' live estimates into the run's cost reading")
    run.add_argument("--reports", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--ceiling-usd", default=os.environ.get("HONUA_CLOUD_COST_CEILING_USD", "20"))
    run.add_argument("--expected-cells", default="",
                     help="comma-separated cells the run dispatched; each must report its cost")
    prior = sub.add_parser("prior-runs", help="Cost Explorer actuals for earlier runs (lagged ~24 h)")
    prior.add_argument("--output", type=Path, required=True)
    prior.add_argument("--ceiling-usd", default=os.environ.get("HONUA_CLOUD_COST_CEILING_USD", "20"))
    prior.add_argument("--lookback-days", type=int, default=3)
    args = parser.parse_args(argv)
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if args.command == "run":
        body = run_cost(_load_reports(args.reports), args.ceiling_usd, run_id=run_id, run_attempt=attempt,
                        expected_cells=[c for c in args.expected_cells.split(",") if c])
    else:
        body = prior_run_costs(CostExplorer(), current_run=run_id, default_ceiling_usd=args.ceiling_usd,
                               lookback_days=args.lookback_days)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in body.items() if k not in ("estimateBasis", "runs")}, indent=2))
    for prior_run in body.get("runs", []):
        print(f"run {prior_run['runId']}: {prior_run['status']} ${prior_run['actualUsd']} "
              f"(settled ${prior_run['settledUsd']}) "
              f"(ceiling ${prior_run['ceilingUsd']}, {prior_run['ceilingSource']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
