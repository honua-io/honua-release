#!/usr/bin/env python3
"""Actual isolated operate loop; engineering proof, never a release qualification."""
from __future__ import annotations

import importlib.util
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SHA = "28a6419fcc9bbce6ef9503ff2b7ba37f47fbf7ee"
WINDOW = 45
BASE = "http://127.0.0.1:19191"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Reviewed fixture anchor changed: {old[:70]}")
    return text.replace(old, new, 1)


def owned_prefix(value):
    if not re.fullmatch(r"operate3302-[a-f0-9]{16}", value):
        raise ValueError("Refusing unowned Docker namespace")
    return value


def assert_pending(proposal, finding_id):
    assert proposal["findingId"] == finding_id
    assert proposal["kind"] == "Deploy"
    assert proposal["status"] == "AwaitingApproval"
    assert not proposal.get("executionOperationId"), "Actuation before approval"
    assert not proposal.get("blockingReasons"), "Proposal is blocked"
    assert proposal.get("requestedBy"), "Missing requester identity"


def assert_completed_window(observed, completed):
    protection = observed["protection"]
    start = dt.datetime.fromisoformat(protection["firstExposureAt"])
    deadline = dt.datetime.fromisoformat(protection["observationDeadline"])
    finish = dt.datetime.fromisoformat(completed["completedAt"])
    assert (deadline - start).total_seconds() == WINDOW
    assert completed["status"] == "Succeeded" and finish >= deadline
    assert completed["operationId"] == observed["operationId"]


def cleanup(run, out):
    if not (run / "ownership.json").exists():
        return
    owner = json.loads((run / "ownership.json").read_text())
    prefix = owned_prefix(owner["prefix"])
    assert owner["runDirectory"] == str(run.resolve())
    names = [f"{prefix}-{suffix}" for suffix in ("server", "telemetry", "redis", "postgres", "app-19181", "app-19182")]
    replicas = subprocess.check_output(["docker", "ps", "-aq", "--filter", f"label=honua.target={prefix}"], text=True).split()
    subprocess.run(["docker", "rm", "-f", *names, *replicas], capture_output=True, check=False)
    subprocess.run(["docker", "network", "rm", f"{prefix}-net"], capture_output=True, check=False)
    subprocess.run(["docker", "image", "rm", *[f"{prefix}-workload:{revision}" for revision in ("prior-a", "candidate-b", "candidate-c")]], capture_output=True, check=False)
    left = subprocess.check_output(["docker", "ps", "-a", "--format", "{{.Names}}"], text=True).split()
    remaining = [name for name in left if name.startswith(prefix + "-")]
    labels = subprocess.check_output(["docker", "ps", "-aq", "--filter", f"label=honua.target={prefix}"], text=True).split()
    networks = subprocess.check_output(["docker", "network", "ls", "--format", "{{.Name}}"], text=True).split()
    passed = not remaining and not labels and f"{prefix}-net" not in networks
    out.mkdir(parents=True, exist_ok=True)
    (out / "cleanup.json").write_text(json.dumps({"status": "passed" if passed else "failed", "prefix": prefix, "remainingContainers": remaining, "remainingOwnedLabels": labels}))
    assert passed, "Owned fixture cleanup incomplete"


class Mcp:
    def __init__(self, key, events):
        self.key, self.events, self.session, self.sequence = key, events, None, 0

    def rpc(self, method, params, notify=False):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            body["id"] = self.sequence
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "X-API-Key": self.key}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(BASE + "/mcp", data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            self.session = response.headers.get("Mcp-Session-Id") or self.session
        if notify:
            return
        messages = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")] or [json.loads(raw)]
        reply = next(message for message in messages if message.get("id") == body["id"])
        self.events.append({"surface": "mcp", "request": body, "reply": reply})
        assert "error" not in reply and not reply.get("result", {}).get("isError"), reply
        return reply["result"]

    def tool(self, name, arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})["structuredContent"]


def main():
    run = Path(os.environ["HONUA_OPERATE_RUN"]).resolve()
    out = Path(os.environ["HONUA_OPERATE_OUT"]).resolve()
    if sys.argv[1:] == ["--cleanup"]:
        cleanup(run, out)
        return 0
    os.umask(0o077)
    run.mkdir(parents=True, exist_ok=False)
    out.mkdir(parents=True, exist_ok=False)
    prefix = owned_prefix("operate3302-" + secrets.token_hex(8))
    (run / "ownership.json").write_text(json.dumps({"prefix": prefix, "runDirectory": str(run)}))
    sys.path.insert(0, str(ROOT / "certification/control-plane-parity"))
    parity = load("parity_helpers", ROOT / "certification/control-plane-parity/run.py")
    image, source = os.environ["HONUA_SERVER_IMAGE"], os.environ["HONUA_SERVER_SOURCE"]
    parity.check_identity(image, source)
    clients = parity.selected_clients(yaml.safe_load((ROOT / "platform-manifest.yaml").read_text()))
    private = []
    env = {key: value for key, value in os.environ.items() if key not in ("GH_TOKEN", "GITHUB_TOKEN", "RELEASE_GH_TOKEN")}
    env.update(PROOF_WORK=str(run), PROOF_PREFIX=prefix, PROOF_SERVER_IMAGE=image)
    report = {"status": "failed", "kind": "joined-operate-engineering-proof", "scope": "Real self-hosted fixture with simulated healthy telemetry and automated distinct approver. Not human approval acceptance, cloud placement, worker execution or release qualification.", "serverImage": image, "serverSource": source, "fixtureSource": FIXTURE_SHA, "prefix": prefix, "observationWindowSeconds": WINDOW, "events": [], "assertions": [], "stages": []}
    log = (run / "execution.log").open("w")

    def command(args, cwd=run, timeout=300, extra=None):
        return subprocess.run(args, cwd=cwd, env={**env, **(extra or {})}, stdout=log, stderr=log, check=True, timeout=timeout)

    def stage(name, action):
        record = {"name": name, "status": "failed"}
        report["stages"].append(record)
        value = action()
        record["status"] = "passed"
        print("PASS: " + name, flush=True)
        return value

    def save():
        # Only explicitly selected receipts; never upload the private fixture directory.
        for name in ("admin-key", "issuer-key"):
            if (run / name).exists():
                private.append((run / name).read_text().strip())
        if (run / "server.env").exists():
            private.extend(line.split("=", 1)[1] for line in (run / "server.env").read_text().splitlines() if "=" in line and any(word in line.upper().split("=", 1)[0] for word in ("KEY", "PASSWORD", "SECRET", "CONNECTIONSTRING")))
        (out / "report.json").write_text(parity.sanitize(json.dumps(report, indent=2), private))
        log.flush()
        (out / "execution.log").write_text(parity.sanitize((run / "execution.log").read_text(), private))

    try:
        stage("verify published client bytes and provenance", lambda: command([sys.executable, str(ROOT / "tools/verify_client_artifacts.py"), "--manifest", str(ROOT / "platform-manifest.yaml"), "--ecosystem", "npm"], extra={"GH_TOKEN": os.environ.get("GH_TOKEN", "")}))
        fixture_repo = ROOT / "_operate-devops"
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=fixture_repo, text=True).strip() == FIXTURE_SHA
        fixture = run / "fixture"
        shutil.copytree(fixture_repo / "certification/protected-recovery", fixture)
        journey = load("protected_journey", fixture / "journey.py")
        journey.PREFIX = journey.TARGET = prefix
        journey.restart_server = lambda work: command(["bash", str(fixture / "boot.sh"), "restart"])
        boot = (fixture / "boot.sh").read_text()
        # All upstream destructive selectors become unique before any fixture invocation.
        assert "proof-selfhosted" in boot
        boot = boot.replace("proof-selfhosted", prefix)
        boot = replace_once(boot, "redis-server --appendonly no", "redis-server --appendonly yes --appendfsync everysec --maxmemory-policy noeviction")
        # These are fixture workload image IDs, not claims of release worker qualification.
        additions = '\nControlPlane__PlatformRelease__Version=3302-proof\nControlPlane__PlatformRelease__ServingArtifactReference=$(docker image inspect -f "{{.Id}}" "${prefix}-workload:candidate-b")\nControlPlane__PlatformRelease__Workers__0__ArtifactReference=$(docker image inspect -f "{{.Id}}" "${prefix}-workload:candidate-b")\n'
        parameters = {**journey.TELEMETRY_PARAMETERS, "deployment.protection.observation_window_seconds": str(WINDOW)}
        additions += "".join(f"ControlPlane__DeployTargets__0__Parameters__{key}={value}\n" for key, value in parameters.items())
        boot = replace_once(boot, '\nENV\n', additions + 'ENV\n')
        (fixture / "boot.sh").write_text(boot)
        stage("pull immutable server", lambda: command(["docker", "pull", image], timeout=600))
        inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
        assert inspected["Config"]["Labels"]["org.opencontainers.image.revision"] == source
        assert any(digest.endswith("@" + image.split("@", 1)[1]) for digest in inspected["RepoDigests"])
        report["imageId"] = inspected["Id"]
        consumer = run / "consumer"
        consumer.mkdir()
        (consumer / "package.json").write_text(json.dumps({"private": True, "type": "module", "dependencies": {name: pin["version"] for name, pin in clients.items()}}))
        (consumer / "expected-clients.json").write_text(json.dumps(clients))
        for name in ("verify-client.mjs", "client-identity.mjs"):
            shutil.copy(ROOT / "certification/control-plane-parity" / name, consumer / name)
        stage("install verified published clients", lambda: command(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org"], cwd=consumer))
        stage("verify installed CLI package identity", lambda: command(["node", "verify-client.mjs"], cwd=consumer))
        report["publishedClients"] = json.loads((consumer / "provenance.json").read_text())
        stage("boot unique real self-hosted deployment fixture", lambda: command(["bash", str(fixture / "boot.sh"), "up"], timeout=600))
        running_id = subprocess.check_output(["docker", "inspect", "--format", "{{.Image}}", prefix + "-server"], text=True).strip()
        assert running_id == report["imageId"]
        j = journey.Journey(BASE, (run / "admin-key").read_text(), str(run))
        j.cell = {"name": "joined-operate", "events": report["events"], "assertions": report["assertions"]}
        # Start from a real completed deployment, so the findings terminal index has provenance.
        j.reset()
        j.telemetry(journey.HEALTHY, "declared simulated healthy Prometheus measurements")
        seed = j.create("candidate-c", prefix + "-seed", extra={"deployment.protection.observation_window_seconds": str(WINDOW)})
        j.watch(seed, lambda op: op.get("status") == "Succeeded", 240, "real prior revision deployment completed")
        j.snapshot("after real seed deployment")
        j.check(j.served() == "candidate-c", "prior workload serves before proposal")
        report["seedOperationId"] = seed
        keys = {}
        for role, grants in (("proposer", ["admin"]), ("approver", ["admin:read", "admin:approve"])):
            status, response = j.http("POST", "/api/v1/admin/api-keys", {"name": prefix + "-" + role, "permissions": grants}, record=False)
            j.check(status in (200, 201), "distinct fixture credential created")
            data = response.get("data", response)
            keys[role] = data["key"]
            private.append(keys[role])
            report[role] = data["apiKey"]
        j.check(report["proposer"]["id"] != report["approver"]["id"], "approver credential differs from proposer")
        mcp = Mcp(keys["proposer"], report["events"])
        mcp.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "joined-operate-proof", "version": "1"}})
        mcp.rpc("notifications/initialized", {}, notify=True)
        findings = mcp.tool("honua_ops_findings", {})
        finding = next(item for item in findings["findings"] if item["rule"] == "platform-release-runtime-divergence" and item["subject"]["targetId"] == prefix)
        report["finding"] = finding
        proposal = mcp.tool("honua_propose_finding", {"findingId": finding["id"], "candidateId": prefix})
        proposal_id = proposal["proposalId"]
        report["proposalId"] = proposal_id

        def cli(role, operation):
            cli_env = {key: env[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in env}
            cli_env.update(HONUA_BASE_URL=BASE, HONUA_API_KEY=keys[role], HONUA_CONFIG_HOME=str(run / "empty-config"))
            args = ["node", str(consumer / "node_modules/@honua/sdk-js/dist/src/cli/bin.js"), "admin", "operate", operation, "--path", "id=" + proposal_id, "--yes", "--json"]
            result = subprocess.run(args, env=cli_env, text=True, capture_output=True, timeout=45)
            body = json.loads((result.stdout or result.stderr).strip())
            report["events"].append({"surface": "published-admin-cli", "principal": role, "operation": operation, "exitCode": result.returncode, "output": body})
            return result.returncode, body

        code, pending = cli("proposer", "getOperationProposal")
        j.check(code == 0, "published authenticated CLI can read sealed proposal")
        assert_pending(pending, finding["id"])
        j.check(j.served() == "candidate-c", "proposal did not change serving workload")
        code, denied = cli("proposer", "approveOperationProposal")
        j.check(code != 0 and re.search(r"HTTP 403\b", denied.get("error", "")), "actual CLI refuses self-approval")
        code, unchanged = cli("proposer", "getOperationProposal")
        j.check(code == 0, "proposal readable after refused approval")
        assert_pending(unchanged, finding["id"])
        j.check(j.served() == "candidate-c", "refused approval did not actuate deployment")
        save()
        code, approved = cli("approver", "approveOperationProposal")
        j.check(code == 0, "separate authorized CLI approver accepted")
        deadline = time.monotonic() + 240
        while True:
            code, resolved = cli("proposer", "getOperationProposal")
            j.check(code == 0, "actual proposal lifecycle remains readable")
            if resolved.get("executionOperationId"):
                break
            assert time.monotonic() < deadline and resolved["status"] not in ("Failed", "Rejected", "Expired")
            time.sleep(2)
        j.check(resolved.get("resolvedBy") != resolved["requestedBy"] and bool(resolved.get("resolvedBy")), "server recorded separate resolving actor")
        operation_id = resolved["executionOperationId"]
        report["executionOperationId"] = operation_id
        observed = j.watch(operation_id, lambda op: journey.phase(op) == "observing", 180, "typed actuator opened protection window")
        j.check(observed["protection"]["candidateRevision"] == j.image("candidate-b"), "protection binds declared candidate bytes")
        j.check(observed["protection"]["previousRevision"] == j.image("candidate-c"), "protection binds actual previous bytes")
        completed = j.watch(operation_id, lambda op: op.get("status") == "Succeeded", 180, "full protection window completed")
        assert_completed_window(observed, completed)
        j.check(j.served() == "candidate-b", "actual proxy serves approved workload after convergence")
        report["terminalOperation"] = completed
        assert re.fullmatch(r"[A-Za-z0-9_-]+", proposal_id)
        query = f"select coalesce(json_agg(t),'[]'::json) from (select audit_id,actor,action,outcome,resource_id,correlation_id,details from honua.audit_log where resource_id='{proposal_id}' order by audit_id)t"
        audits = json.loads(subprocess.check_output(["docker", "exec", prefix + "-postgres", "psql", "-U", "postgres", "-d", "honua", "-Atc", query], text=True))
        proposed = [row for row in audits if row["action"] == "operation.proposed"]
        applied = [row for row in audits if row["action"] == "operation.applied"]
        j.check(len(proposed) == len(applied) == 1, "one durable proposed and applied audit join")
        j.check(proposed[0]["actor"] == pending["requestedBy"] and applied[0]["actor"] == resolved["resolvedBy"], "durable audit retains proposer and distinct approver")
        j.check(proposed[0]["correlation_id"] == applied[0]["correlation_id"], "proposal and application share durable correlation")
        j.check(all(json.loads(row["details"])["findingId"] == finding["id"] for row in (proposed[0], applied[0])), "audit traces application to original finding")
        report["durableProposalAudit"] = audits
        after = mcp.tool("honua_ops_findings", {})
        j.check(not any(item["id"] == finding["id"] for item in after["findings"]), "original deterministic divergence finding cleared")
        report["status"] = "passed"
    except Exception as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        for name in (prefix + "-server", prefix + "-app-19181", prefix + "-app-19182"):
            try:
                subprocess.run(["docker", "logs", "--tail", "80", name], stdout=log, stderr=log, timeout=30, check=False)
            except (OSError, subprocess.TimeoutExpired):
                log.write("Bounded diagnostic unavailable\n")
        print("FAIL: joined operate proof; sanitized receipt retained", flush=True)
    finally:
        save()
        log.close()
        try:
            cleanup(run, out)
        except Exception as error:
            report["status"] = "failed"
            report["cleanupFailure"] = str(error)
            (out / "report.json").write_text(parity.sanitize(json.dumps(report, indent=2), private))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
