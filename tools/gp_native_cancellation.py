"""Independent AWS native cancellation/effects producer (release679).

No resource, task definition, permission, barrier or release policy is mutated.
The only actuations are installed-MCP plan submission and repeated cancellation.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "e2e"))
from runner.update_rollback import Api, ProofError, checked, require

SCHEMA = "honua.aws-native-gp-cancellation/v1"
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
# GeoprocessingOutputStagingOptions.StoreReference is an opaque store identity,
# not a job or worker token. Keep its production character and length contract.
STORE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")
# The reviewed native image provides this workload in protected image roots.
# Neither operator configuration nor an observer-selected arbitrary path may
# redefine the executable that satisfies the gdal.ogr2ogr qualification cell.
NATIVE_OGR_EXECUTABLES = frozenset(("/usr/bin/ogr2ogr", "/usr/local/bin/ogr2ogr"))
CLIENT_LOCK = "f2d66687dd7c31b6620d1417cabaa367da9b56bed7bcb00f56aabcf96e789192"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def one(rows, predicate, reason):
    found = [row for row in rows if predicate(row)]
    require(len(found) == 1, reason)
    return found[0]


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "timestamp timezone missing")
    return result.timestamp()


def inventory(value, *, empty=True):
    require(isinstance(value, dict) and value.get("state") in ("present", "absent")
            and isinstance(value.get("files"), list) and len(value["files"]) <= 4096
            and type(value.get("bytes")) is int and 0 <= value["bytes"] <= 128 * 1024 * 1024,
            "physical job outputs/scratch remain or observation incomplete")
    require(not empty or value["files"] == [] and value["bytes"] == 0, "physical job effects remain")
    require(value["state"] != "absent" or value["files"] == [] and value["bytes"] == 0, "contradictory absent inventory")
    if value["state"] == "present":
        require(type(value.get("device")) is int and value["device"] >= 0 and type(value.get("inode")) is int and value["inode"] > 0,
                "physical scope identity missing")
    paths, total = set(), 0
    for row in value["files"]:
        require(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256"}
                and isinstance(row["path"], str) and row["path"] and not row["path"].startswith("/")
                and all(part not in ("", ".", "..") for part in row["path"].split("/"))
                and row["path"] not in paths and type(row["bytes"]) is int and row["bytes"] >= 0
                and HEX.fullmatch(str(row["sha256"])), "partial physical inventory")
        paths.add(row["path"])
        total += row["bytes"]
    require(total == value["bytes"], "physical byte count mismatch")
    return value


def observation(value, *, job, nonce, phase, start, end, policy_sha, store):
    require(isinstance(value, dict) and value.get("schema") == "honua.gp-native-observation/v1"
            and value.get("status") == "observed" and value.get("qualification") is False
            and value.get("jobId") == job and value.get("nonce") == nonce and value.get("phase") == phase,
            "missing/wrong native observation")
    at = value.get("observedAtUnixNs")
    require(type(at) is int and start - 2_000_000_000 <= at <= end + 2_000_000_000
            and end - start <= 60_000_000_000, "stale/slow native observation")
    require(value.get("policySha256") == policy_sha and value.get("store") == store,
            "observer policy/store identity changed")
    inventory(value.get("outputInventory"))
    if phase in ("before", "started", "after"):
        barrier = value.get("barrier", {})
        require(isinstance(barrier, dict)
                and barrier.get("nativeEntryBoundary") == "native-exec-entry-paused"
                and all(isinstance(barrier.get(key), str) and HEX.fullmatch(barrier[key])
                        for key in ("sha256", "readySha256", "nativeEntrySha256")),
                "actual native executable entry evidence missing")
        executable = barrier.get("nativeExecutable")
        require(isinstance(executable, str) and executable.startswith("/")
                and not executable.startswith("//") and "\x00" not in executable
                and ".." not in executable.split("/") and str(PurePosixPath(executable)) == executable
                and PurePosixPath(executable).name,
                "native executable identity missing/noncanonical")
        require(executable in NATIVE_OGR_EXECUTABLES,
                "native entry executable is not the image-bound ogr2ogr workload")
        require(barrier["sha256"] == sha((barrier["readySha256"] + "\n" + barrier["nativeEntrySha256"]).encode("ascii")),
                "native entry and ready receipts are not jointly bound")
    if phase in ("before", "started"):
        barrier = value.get("barrier", {})
        require(barrier.get("operationId") == job and HEX.fullmatch(str(barrier.get("sha256")))
                and TOKEN.fullmatch(str(barrier.get("workerId"))) and type(barrier.get("childProcessId")) is int
                and barrier["childProcessId"] > 0 and type(barrier.get("workerProcessId")) is int
                and barrier["workerProcessId"] > 0 and start / 1e9 - 300 <= timestamp(barrier["readyAt"]) <= end / 1e9,
                "native entry is absent/stale/unbound")
    if phase in ("before", "after"):
        process = value.get("process", {})
        keys = {"workerId", "worker", "child", "bootId", "pidNamespace", "barrierSha256", "scratchRelative"}
        require(set(process) == keys | ({"childAbsent"} if phase == "after" else set())
                and re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", str(process.get("bootId")))
                and type(process.get("pidNamespace")) is int and process["pidNamespace"] > 0
                and HEX.fullmatch(str(process.get("barrierSha256")))
                and isinstance(process.get("scratchRelative"), str)
                and all(TOKEN.fullmatch(part) for part in process["scratchRelative"].split("/")), "partial process observation")
        for key in ("worker", "child"):
            item = process.get(key, {})
            require(isinstance(item, dict) and set(item) == {"pid", "parentPid", "startTicks"}
                    and all(type(item[name]) is int for name in item)
                    and item["pid"] > 0 and item["parentPid"] >= 0 and item["startTicks"] > 0,
                    "partial native PID/start identity")
        barrier = value.get("barrier", {})
        require(process["child"]["parentPid"] == process["worker"]["pid"]
                and process["child"]["startTicks"] >= process["worker"]["startTicks"]
                and process["workerId"] == barrier.get("workerId") and process["barrierSha256"] == barrier.get("sha256")
                and process["child"]["pid"] == barrier.get("childProcessId")
                and process["worker"]["pid"] == barrier.get("workerProcessId"), "native/barrier process identity mismatch")
        scratch = inventory(value.get("scratchInventory"), empty=phase == "after")
        require(phase != "before" or scratch["state"] == "present", "native scratch scope missing")
        require(phase != "after" or process.get("childAbsent") is True, "native child cessation missing")
    return value


def efs_mount(definition, container, path, *, readonly):
    mount = one(container.get("mountPoints", []), lambda row: row.get("containerPath") == path,
                "required exact mount missing/ambiguous")
    require(mount.get("readOnly", False) is readonly, "mount read-only contract violated")
    volume = one(definition.get("volumes", []), lambda row: row.get("name") == mount.get("sourceVolume"),
                 "mount source missing/ambiguous")
    efs = volume.get("efsVolumeConfiguration", {})
    require(efs.get("fileSystemId") and efs.get("transitEncryption") == "ENABLED"
            and efs.get("authorizationConfig", {}).get("accessPointId"), "attested shared EFS mount required")
    return {"fileSystemId": efs["fileSystemId"], "accessPointId": efs["authorizationConfig"]["accessPointId"],
            "rootDirectory": efs.get("rootDirectory", "/")}


def mount_floor(container):
    """Neither image code nor any observed subtree may be replaced by a submount."""
    require(not container.get("volumesFrom"), "inherited mounts cannot authenticate code/effect scope")
    linux = container.get("linuxParameters", {})
    require(not linux.get("tmpfs") and not linux.get("devices"), "undeclared tmpfs/device mounts can shadow image/effect scope")
    paths = []
    protected = ("/app", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/etc", "/proc", "/sys", "/dev")
    def overlap(left, right):
        return left == right or left.startswith(right + "/") or right.startswith(left + "/")
    for mount in container.get("mountPoints", []):
        path = mount.get("containerPath")
        require(isinstance(path, str) and path.startswith("/") and not path.startswith("//")
                and path != "/" and ".." not in path.split("/")
                and str(PurePosixPath(path)) == path, "noncanonical mount path")
        require(not any(overlap(path, code) for code in protected), "mount shadows authenticated image code/imports")
        require(not any(overlap(path, previous) for previous in paths), "nested/overlapping filesystem mount shadows")
        paths.append(path)
    return set(paths)


def bootstrap_environment(entries):
    result = {item["name"].replace(":", "__").lower(): item.get("value") for item in entries}
    require(len(result) == len(entries), "ambiguous environment precedence")
    require(not any(key in {"path", "env", "bash_env", "glibc_tunables", "gdal_driver_path", "gdal_python_driver_path"}
                    or key.startswith(("ld_", "python", "dotnet_startup", "dotnet_additional", "dotnet_shared", "coreclr_", "complus_"))
                    for key in result), "environment overrides authenticated executable/imports")
    return result


class AwsObserver:
    def __init__(self, config, *, run=subprocess.run, clock=time.time_ns):
        self.config, self.run, self.clock = config, run, clock
        require(config.get("cell") in ("aws-ecs/redis-on", "aws-serverless/redis-on"), "unsupported lifecycle cell")
        require(re.fullmatch(r"[a-f0-9]{40}", str(config.get("source"))), "full immutable server source required")
        require(re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", str(config.get("workerImage"))), "immutable native image required")
        require(HEX.fullmatch(str(config.get("policySha256"))) and isinstance(config.get("store"), dict)
                and set(config["store"]) == {"reference", "configurationDigest", "attestationSha256"}
                and isinstance(config["store"]["reference"], str)
                and STORE_REFERENCE.fullmatch(config["store"]["reference"])
                and all(HEX.fullmatch(str(config["store"][k])) for k in ("configurationDigest", "attestationSha256")),
                "governed observer policy/store binding required")
        self.expected_digest = config["workerImage"].rsplit("@", 1)[1]
        self.authenticated = False

    def aws(self, *args):
        return json.loads(checked(self.run, ["aws", *args, "--region", self.config["region"], "--output", "json"],
                                  "AWS observation unavailable", timeout=60))

    def task(self, arn, container_name, *, running=True):
        response = self.aws("ecs", "describe-tasks", "--cluster", self.config["cluster"], "--tasks", arn)
        require(not response.get("failures"), "provider task is missing")
        task = one(response.get("tasks", []), lambda row: row.get("taskArn") == arn, "exact task unavailable")
        require(task.get("clusterArn") == self.config["cluster"] and task.get("lastStatus") == ("RUNNING" if running else "STOPPED"),
                "task state/cluster changed")
        container = one(task.get("containers", []), lambda row: row.get("name") == container_name, "exact container missing")
        require(container.get("runtimeId") and container.get("containerArn")
                and container.get("lastStatus") == task["lastStatus"], "container/image identity mismatch")
        definition = self.aws("ecs", "describe-task-definition", "--task-definition", task["taskDefinitionArn"])["taskDefinition"]
        require(definition.get("taskDefinitionArn") == task["taskDefinitionArn"], "definition identity mismatch")
        spec = one(definition.get("containerDefinitions", []), lambda row: row.get("name") == container_name,
                   "container definition missing")
        require(spec.get("image") == self.config["workerImage"], "task definition native image is not immutable")
        architecture = {"X86_64": "amd64", "ARM64": "arm64"}.get(
            definition.get("runtimePlatform", {}).get("cpuArchitecture", "X86_64"))
        require(architecture and container.get("imageDigest") == self.runtime_digests.get(architecture),
                "running container is not an architecture member of authenticated native image")
        return task, container, definition, spec

    def authenticate(self):
        from check_image_attestation import check, AttestationMismatch
        result = checked(self.run, ["gh", "attestation", "verify", "oci://" + self.config["workerImage"],
                         "--repo", "honua-io/honua-server", "--format", "json", "--source-digest", self.config["source"],
                         "--signer-workflow", "honua-io/honua-server/.github/workflows/nightly-container-build.yml"],
                         "native image source provenance refused")
        try:
            check(json.loads(result), self.config["source"], self.expected_digest)
        except AttestationMismatch as exc:
            raise ProofError("native image source provenance mismatch") from exc
        raw = checked(self.run, ["docker", "buildx", "imagetools", "inspect", self.config["workerImage"], "--raw"],
                      "authenticated native manifest unavailable")
        require("sha256:" + sha(raw.encode()) == self.expected_digest, "native manifest bytes differ from attested digest")
        manifest = json.loads(raw)
        self.runtime_digests = {}
        if "manifests" in manifest:
            for arch in ("amd64", "arm64"):
                children = [row for row in manifest["manifests"] if row.get("platform", {}).get("os") == "linux"
                            and row.get("platform", {}).get("architecture") == arch]
                if children:
                    require(len(children) == 1 and re.fullmatch(r"sha256:[a-f0-9]{64}", children[0].get("digest", "")),
                            "ambiguous native architecture membership")
                    self.runtime_digests[arch] = children[0]["digest"]
        else:
            self.runtime_digests = {"amd64": self.expected_digest, "arm64": self.expected_digest}
        self.provenance = {"source": self.config["source"], "image": self.config["workerImage"],
                           "verifiedAttestationSha256": sha(result.encode()), "manifestSha256": sha(raw.encode()),
                           "runtimeDigests": self.runtime_digests.copy()}
        self.authenticated = True

    def preflight(self):
        require(self.authenticated, "native provenance must be authenticated before AWS operations")
        config = self.config
        if config["cell"] == "aws-serverless/redis-on" and not config.get("workerTask"):
            self.observer = self.task(config["observerTask"], config["observerContainer"])
            return {"observer": self.identity(self.observer), "worker": "pending-provider-attempt", "nativeProvenance": self.provenance}
        worker = self.task(config["workerTask"], config["workerContainer"])
        observer = self.task(config["observerTask"], config["observerContainer"])
        wt, wc, wd, ws = worker
        ot, oc, od, ospec = observer
        require(ot.get("enableExecuteCommand") is True and any(agent.get("name") == "ExecuteCommandAgent"
                and agent.get("lastStatus") == "RUNNING" for agent in oc.get("managedAgents", [])), "Exec agent unavailable")
        require(ws.get("readonlyRootFilesystem") is True and ospec.get("readonlyRootFilesystem", False) is False,
                "worker root must remain read-only; Exec belongs in diagnostic container")
        require(ws.get("user", "1001:1001") in ("1001", "1001:1001") and not ws.get("privileged", False),
                "native worker must retain image UID1001 and non-privileged execution")
        mount_floor(ws)
        observer_paths = mount_floor(ospec)
        expected_paths = {config["outputRoot"], config["barrierRoot"], "/run/honua/observer-policy"}
        if config["cell"] == "aws-ecs/redis-on":
            expected_paths.add(config["scratchRoot"])
        require(observer_paths == expected_paths, "observer has undeclared filesystem mounts")
        require(not ospec.get("environment") and not ospec.get("environmentFiles") and not ospec.get("secrets")
                and not ospec.get("privileged", False), "observer launch environment is not isolated")
        observer_overrides = [row for row in ot.get("overrides", {}).get("containerOverrides", [])
                              if row.get("name") == config["observerContainer"]]
        require(len(observer_overrides) <= 1 and all(not row.get("command") and not row.get("environment")
                and not row.get("environmentFiles") for row in observer_overrides), "observer launch override refused")
        env = bootstrap_environment(ws.get("environment", []))
        secret_names = bootstrap_environment([{"name": item["name"], "value": None} for item in ws.get("secrets", [])])
        require(not set(secret_names) & set(env) and not any(key.startswith("honua_gp_qualification_") for key in secret_names),
                "secret environment shadows qualification/bootstrap configuration")
        require(len(env) == len(ws.get("environment", [])) and not ws.get("command")
                and not ws.get("environmentFiles")
                and ws.get("entryPoint", ["dotnet", "Honua.Worker.Gdal.dll"]) == ["dotnet", "Honua.Worker.Gdal.dll"],
                "worker configuration precedence is ambiguous")
        overrides = [row for row in wt.get("overrides", {}).get("containerOverrides", [])
                     if row.get("name") == config["workerContainer"]]
        require(len(overrides) <= 1, "ambiguous worker task overrides")
        if overrides:
            override = overrides[0]
            require(not override.get("command") and not override.get("environmentFiles"), "opaque worker launch overrides refused")
            entries = override.get("environment", [])
            selected = bootstrap_environment(entries)
            require(len(selected) == len(entries), "ambiguous worker environment override")
            env.update(selected)
        require(env.get("honua_gp_qualification_barrier_root") == config["barrierRoot"]
                and env.get("honua_gp_qualification_pause_at") == "native-process-started"
                and env.get("honua_gp_qualification_executor_mode") != "ignore-cancellation", "native-only cancellation staging missing")
        required_env = {"geoprocessing__outputstaging__provider": "local",
                        "geoprocessing__outputstaging__localrootpath": config["outputRoot"],
                        "geoprocessing__outputstaging__keyprefix": config["keyPrefix"],
                        "geoprocessing__outputstaging__storereference": config["store"]["reference"],
                        "geoprocessing__outputstaging__persistenceclass": "shared-persistent",
                        "gdalworker__scratchroot": config["scratchRoot"]}
        require(all(env.get(key) == value for key, value in required_env.items()) and not any(
            item.get("name", "").replace(":", "__").lower() in required_env for item in ws.get("secrets", [])),
            "worker output/scratch configuration does not match independent mount scope")
        require(ospec.get("entryPoint") == ["/usr/bin/python3"]
                and ospec.get("command") == ["-I", "-B", "-c", "import time; time.sleep(86400)"],
                "diagnostic container must not execute a second worker")
        policy_mount = one(ospec.get("mountPoints", []), lambda row: row.get("containerPath") == "/run/honua/observer-policy",
                           "separate policy mount missing")
        require(policy_mount.get("readOnly") is True and not any(m.get("sourceVolume") == policy_mount.get("sourceVolume")
                for m in ws.get("mountPoints", [])), "job-writable policy mount refused")
        volumes = {}
        for path in (config["outputRoot"], config["barrierRoot"]):
            volumes[path] = efs_mount(wd, ws, path, readonly=False)
            require(efs_mount(od, ospec, path, readonly=True) == volumes[path], "worker/observer EFS identity mismatch")
        if config["cell"] == "aws-ecs/redis-on":
            require(wt["taskArn"] == ot["taskArn"] and config["workerContainer"] != config["observerContainer"]
                    and wd.get("pidMode") == "task", "worker task PID namespace sidecar required")
            path = config["scratchRoot"]
            wm = one(ws.get("mountPoints", []), lambda row: row.get("containerPath") == path, "scratch mount missing")
            om = one(ospec.get("mountPoints", []), lambda row: row.get("containerPath") == path, "observer scratch mount missing")
            require(wm.get("readOnly", False) is False and om.get("readOnly") is True
                    and wm.get("sourceVolume") == om.get("sourceVolume"), "scratch mount identity mismatch")
        else:
            require(wt["taskArn"] != ot["taskArn"], "Batch effects observer must survive worker task cessation")
        self.worker, self.observer = worker, observer
        return {"worker": self.identity(worker), "observer": self.identity(observer), "volumes": volumes,
                "nativeProvenance": self.provenance}

    def bind_batch(self, job, provider):
        actual = one(self.aws("batch", "describe-jobs", "--jobs", provider).get("jobs", []),
                     lambda row: row.get("jobId") == provider, "exact Batch job unavailable")
        attempts = actual.get("attempts", [])
        require(len(attempts) == 1 and attempts[0].get("container", {}).get("taskArn"),
                "running Batch attempt task unavailable")
        self.config["workerTask"] = attempts[0]["container"]["taskArn"]
        result = self.preflight()
        self.batch(job, provider)
        return result

    @staticmethod
    def identity(view):
        task, container, _, _ = view
        return {"taskArn": task["taskArn"], "taskDefinitionArn": task["taskDefinitionArn"],
                "containerArn": container["containerArn"], "runtimeId": container["runtimeId"], "imageDigest": container["imageDigest"]}

    def observe(self, job, phase, expected=None):
        config, nonce = self.config, secrets.token_hex(16)
        current = self.task(config["observerTask"], config["observerContainer"])
        require(self.identity(current) == self.identity(self.observer), "observer container replaced")
        args = ["/usr/bin/python3", "-I", "-B", "/app/gp_native_observer.py", "--policy", "/run/honua/observer-policy/observer-policy.json",
                "--job", job, "--nonce", nonce, "--phase", phase]
        if expected is not None:
            args += ["--expected", base64.urlsafe_b64encode(json.dumps(expected).encode()).decode()]
        start = self.clock()
        raw = checked(self.run, ["aws", "ecs", "execute-command", "--region", config["region"], "--cluster", config["cluster"],
                      "--task", config["observerTask"], "--container", config["observerContainer"], "--interactive",
                      "--command", shlex.join(args)], "independent native observation refused", timeout=60)
        end = self.clock()
        require(len(raw) <= 1024 * 1024, "oversized observation")
        records = []
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("schema") == "honua.gp-native-observation/v1":
                records.append(value)
        require(len(records) == 1, "missing/duplicate observer response")
        result = observation(records[0], job=job, nonce=nonce, phase=phase, start=start, end=end,
                             policy_sha=config["policySha256"], store=config["store"])
        scope = {key: config[key] for key in ("barrierRoot", "outputRoot", "keyPrefix")}
        scope.update(role="worker" if config["cell"].startswith("aws-ecs") else "store-observer",
                     scratchRoot=config["scratchRoot"] if config["cell"].startswith("aws-ecs") else None)
        require(result.get("scopeSha256") == sha(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()),
                "observer policy does not inventory the bound worker filesystem scope")
        return result

    def batch(self, job, provider, *, stopped=False):
        actual = one(self.aws("batch", "describe-jobs", "--jobs", provider).get("jobs", []),
                     lambda row: row.get("jobId") == provider, "exact Batch provider job missing")
        require(actual.get("jobQueue") == self.config["batchQueue"] and actual.get("jobDefinition") == self.config["batchDefinition"],
                "Batch queue/definition mismatch")
        env = {row["name"]: row.get("value") for row in actual.get("container", {}).get("environment", [])}
        require(env.get("HONUA_OPERATION_ID") == job and env.get("HONUA_EXECUTION_ATTEMPT") == "1",
                "Batch reserved job/attempt launch identity missing")
        attempts = actual.get("attempts", [])
        require(len(attempts) == 1, "Batch attempt missing/duplicated")
        attempt = attempts[0]
        require(attempt.get("container", {}).get("taskArn") == self.config["workerTask"]
                and type(attempt.get("startedAt")) is int and attempt["startedAt"] > 0, "Batch attempt/task start missing")
        if stopped:
            require(actual.get("status") in ("FAILED", "SUCCEEDED") and type(attempt.get("stoppedAt")) is int
                    and attempt["stoppedAt"] >= attempt["startedAt"]
                    and type(attempt.get("container", {}).get("exitCode")) is int, "Batch cancellation task exit missing")
            task = self.task(self.config["workerTask"], self.config["workerContainer"], running=False)
            require(self.identity(task) == self.identity(self.worker), "Batch container identity changed")
        else:
            require(actual.get("status") == "RUNNING" and not attempt.get("stoppedAt"), "native Batch task not running before cancel")
        return {"providerJobId": provider, "attempt": 1, "taskArn": self.config["workerTask"], "status": actual["status"],
                "startedAt": attempt["startedAt"], "stoppedAt": attempt.get("stoppedAt"),
                "exitCode": attempt.get("container", {}).get("exitCode")}


class InstalledMcp:
    def __init__(self, endpoint, credential, *, run=subprocess.run):
        self.endpoint, self.credential, self.run = endpoint, credential, run
        self.submitted_job = None
        lock = ROOT / "certification/sources/geospatial-mcp/package-lock.json"
        require(sha(lock.read_bytes()) == CLIENT_LOCK, "maintained MCP client lock changed without review")

    def call(self, mode, argument):
        raw = checked(self.run, ["node", str(ROOT / "tools/gp_native_mcp.mjs"), self.endpoint, mode],
                      "installed MCP lifecycle operation refused", input=json.dumps(argument),
                      env={**os.environ, "HONUA_GP_MCP_API_KEY": self.credential}, timeout=60)
        value = json.loads(raw)
        require(value.get("schema") == "honua.gp-native-mcp/v1" and value.get("clientLockSha256") == CLIENT_LOCK
                and value.get("executionCessationProven") is False, "installed MCP client identity missing")
        if mode == "submit" and TOKEN.fullmatch(str(value.get("jobId"))):
            self.submitted_job = value["jobId"]
        return value


def run_cell(config, observer, api, mcp, plan, *, sleep=time.sleep, clock=time.time, polls=60, late_seconds=15):
    require(config.get("cell") in ("aws-ecs/redis-on", "aws-serverless/redis-on") and 15 <= late_seconds <= 30,
            "required cell/late-window contract")
    observer.authenticate()
    initial = observer.preflight()
    submitted_at = clock()
    def within_budget():
        require(clock() - submitted_at <= 210, "native qualification polling budget exhausted")

    submission = mcp.call("submit", plan)
    job = submission.get("jobId")
    require(TOKEN.fullmatch(str(job)) and submission.get("status") in ("Queued", "Running"), "MCP submission missing real job")
    before = row = None
    for _ in range(polls):
        within_budget()
        row = api.get("/api/v1/admin/jobs/" + job)
        require(row.get("jobId") == job and row.get("status") in ("Queued", "Provisioning", "Running")
                and type(row.get("attemptCount")) is int and row["attemptCount"] <= 1
                and timestamp(row["createdAt"]) >= submitted_at - 2, "job completed/retried/stale before native observation")
        if row["status"] == "Running" and row["attemptCount"] == 1:
            try:
                if config["cell"] == "aws-serverless/redis-on" and row.get("providerOperationId"):
                    initial = observer.bind_batch(job, row["providerOperationId"])
                before = observer.observe(job, "before" if config["cell"] == "aws-ecs/redis-on" else "started")
                break
            except ProofError:
                pass
        sleep(1)
    require(before is not None, "native work never independently observed")
    require(before.get("barrier", {}).get("workerId") == row.get("claimedBy"), "native worker is not canonical claim owner")
    if config["cell"] == "aws-ecs/redis-on":
        require(before.get("barrier", {}).get("workerId") == row.get("claimedBy")
                and before.get("process", {}).get("workerId") == row.get("claimedBy"), "native worker is not canonical claim owner")
    else:
        require(row.get("providerOperationId"), "canonical Batch provider identity missing")
        provider_before = observer.batch(job, row["providerOperationId"])
    ack = mcp.call("cancel", {"jobId": job})
    require(ack.get("jobId") == job and len(ack.get("responses", [])) == 2 and all(r == {
        "jobId": job, "status": "cancellation_requested", "cancellationRequested": True} for r in ack["responses"]),
        "repeated installed cancellation acknowledgement missing")
    for _ in range(polls):
        within_budget()
        terminal = api.get("/api/v1/admin/jobs/" + job)
        require(terminal.get("jobId") == job and type(terminal.get("attemptCount")) is int and terminal["attemptCount"] == 1
                and terminal.get("status") not in ("Succeeded", "Failed"), "contradictory/retried terminal state")
        if terminal.get("status") == "Cancelled":
            break
        sleep(1)
    else:
        raise ProofError("canonical cancellation did not settle")
    after = late = provider_after = None
    for _ in range(polls):
        within_budget()
        try:
            if config["cell"] == "aws-serverless/redis-on":
                provider_after = observer.batch(job, row["providerOperationId"], stopped=True)
            after = observer.observe(job, "after" if config["cell"] == "aws-ecs/redis-on" else "store", before.get("process"))
            break
        except ProofError:
            sleep(1)
    require(after is not None, "native cessation/effects never independently observed")
    if config["cell"] == "aws-ecs/redis-on":
        require(after.get("barrier") == before.get("barrier"), "native entry receipt changed after cancellation")
        require(after.get("process") == {**before["process"], "childAbsent": True}, "original native identity did not cease")
        inventory(after.get("scratchInventory"))
    for stage in ("terminal", "late"):
        if stage == "late":
            sleep(late_seconds)
        current = api.get("/ogc/processes/jobs/" + job)
        canonical = api.get("/api/v1/admin/jobs/" + job)
        require(canonical.get("jobId") == job and canonical.get("status") == "Cancelled"
                and type(canonical.get("attemptCount")) is int and canonical["attemptCount"] == 1
                and canonical.get("providerOperationId") == row.get("providerOperationId")
                and canonical.get("artifactCount", 0) == 0, "late canonical state/attempt/artifacts changed")
        status, result = api.request("GET", "/ogc/processes/jobs/" + job + "/results")
        require(current.get("jobID", current.get("jobId")) == job and current.get("status") == "dismissed"
                and status in (404, 410) and not result.get("outputs") and not result.get("results"), "cancelled/late result remains readable")
        if stage == "late":
            late = observer.observe(job, "after" if config["cell"] == "aws-ecs/redis-on" else "store", before.get("process"))
            require(late.get("store") == after.get("store"), "late store identity changed")
            if config["cell"] == "aws-ecs/redis-on":
                require(late.get("barrier") == before.get("barrier"), "late native entry receipt changed")
                require(late.get("process") == after.get("process"), "late native process identity changed")
                inventory(late.get("scratchInventory"))
            else:
                require(observer.batch(job, row["providerOperationId"], stopped=True) == provider_after, "late Batch attempt changed")
    require(clock() - submitted_at <= 240, "qualification exceeded fresh barrier budget")
    return {"schema": SCHEMA, "status": "pass", "candidateQualified": False, "cell": config["cell"], "jobId": job,
            "source": config["source"], "workerImage": config["workerImage"], "initial": initial,
            "workloadFileSha256": config.get("workloadSha256"), "submittedPlanSha256": sha(json.dumps(plan, sort_keys=True).encode()),
            "submission": submission, "acknowledgement": ack,
            "before": before, "after": after, "late": late, "batchBefore": provider_before if config["cell"].startswith("aws-serverless") else None,
            "batchAfter": provider_after, "lateSeconds": late_seconds}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="submit and cancel a real plan in a prepared qualification cell")
    args = parser.parse_args()
    receipt = {"schema": SCHEMA, "status": "blocked", "candidateQualified": False}
    mcp = None
    try:
        require(args.live, "explicit live qualification invocation required")
        config_bytes = args.config.read_bytes()
        config = json.loads(config_bytes)
        raw = args.plan.read_bytes()
        require(len(raw) <= 16 * 1024 * 1024, "workload size bound")
        require(sha(raw) == config.get("workloadSha256"), "reviewed workload byte binding required")
        plan = json.loads(raw)
        require(set(plan) == {"plan"} and isinstance(plan["plan"], dict) and isinstance(plan["plan"].get("steps"), list)
                and len(plan["plan"]["steps"]) == 1 and plan["plan"]["steps"][0].get("kind") == "Geoprocess"
                and plan["plan"]["steps"][0].get("processId") == "gdal.ogr2ogr", "reviewed filesystem-only native conversion required")
        key = os.environ.get("HONUA_GP_MCP_API_KEY", "")
        api = Api(config["endpoint"], key)
        mcp = InstalledMcp(config["endpoint"], key)
        receipt = run_cell(config, AwsObserver(config), api, mcp, plan)
        receipt["configurationSha256"] = sha(config_bytes)
        receipt["producerSha256"] = sha(Path(__file__).read_bytes())
    except (ProofError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        receipt["reason"] = "native-cancellation-proof-incomplete"
    finally:
        if receipt["status"] != "pass" and mcp is not None and mcp.submitted_job:
            receipt["jobId"] = mcp.submitted_job
            try:
                cleanup = mcp.call("cancel", {"jobId": mcp.submitted_job})
                receipt["cleanupCancellationRequested"] = cleanup.get("jobId") == mcp.submitted_job
            except (ProofError, OSError, ValueError, TypeError, KeyError):
                receipt["cleanupCancellationRequested"] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
