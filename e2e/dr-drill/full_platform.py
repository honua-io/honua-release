#!/usr/bin/env python3
"""Full-platform disaster-recovery drill on the local-docker candidate topology.

`run.sh` is the PostgreSQL restore seam: one store, a fixture database, and a receipt scoped
`postgresql-restore` that `tools/validate_dr_receipt.py` rejects by design. This drill produces
the `honua.dr-drill-receipt/v2` receipt that gate-dr requires. For EVERY substrate the candidate
manifest declares enabled it:

  1. writes durable state through the real product surface,
  2. observes that state through the real runtime surface and hashes what it observed,
  3. backs the substrate up through its own supported backup path, recording id and SHA-256,
  4. destroys the primary state (the named volume, not a graceful stop),
  5. restores the backup into a freshly created, verified-empty store,
  6. restarts, proves the restarted runtime is a different instance, and re-reads the same
     state through the same runtime surface, requiring an identical identity/count/checksum.

RPO and RTO are measured from those real timestamps, never declared: RTO is the window from the
earliest destruction to the latest post-restart read, which is exactly the window the validator
recomputes from the receipt's own observations.

Substrate -> product write surface / runtime read surface on this topology:

  postgresql            OGC API Features insert        -> GET /ogc/features/collections/{c}/items
  transactional-outbox  the same insert's outbox row   -> honua.feature_change_outbox over SQL
  redis                 the same insert's change event -> GET /api/v1/admin/feature-events/replay
  object-storage        GeoServices addAttachment      -> GET .../attachments/{id} (bytes)
  job-queue             GeoServices GP submitJob       -> GET .../GPServer/{task}/jobs/{id}
  workflow-cursors      workflow package -> Schedule   -> the durable orchestration definition

Usage: python3 e2e/dr-drill/full_platform.py [--output DIR] [--keep]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resp import Resp  # noqa: E402  (local helper, imported after sys.path setup)

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "e2e" / "dr-drill" / "compose.full-platform.yml"
MANIFEST = ROOT / "platform-manifest.yaml"
ADMIN_KEY = os.environ.get("HONUA_DR_ADMIN_KEY", "honua-dr-drill-admin-key")
PROJECT = os.environ.get("HONUA_DR_PROJECT", f"honua-dr-full-{os.environ.get('GITHUB_RUN_ID', 'local')}")
SERVICE = "dr-sentinel"
IMPORT_TABLE = "dr_sentinel"
LAYER_ID = 1
PACKAGE_ID = "dr-drill-cursor"
PUBLICATION_ID = "dr-drill-cursor-pub"
GP_TASK = "geometry.buffer"
# Point(-156.5 20.9) as little-endian WKB, the geometry.buffer task's required input.
GP_WKB = base64.b64encode(bytes.fromhex("010100000000000000009063c06666666666e63440")).decode()

# Logical Redis slices. Each declared Redis-backed substrate owns its key space, is exported as
# its own backup artifact, and is restored from that artifact alone: one physical store does not
# collapse three logical recovery obligations into one.
REDIS_SLICES = {
    "redis": ["featurechange:*"],
    "job-queue": ["controlplane:job:*", "honua:universal:*", "universal:progress*"],
    "workflow-cursors": ["orchestration:*"],
}


def log(message: str) -> None:
    print(f"[dr-drill] {message}", flush=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sha256_hex(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def run(*args, check=True, capture=True, stdin=None, timeout=900):
    proc = subprocess.run(list(args), check=False, input=stdin,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, timeout=timeout)
    if check and proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")
        out = (proc.stdout or b"").decode("utf-8", "replace")
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args)}\n{out}\n{err}")
    return proc


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Stack:
    """The composed candidate topology, plus the substrate surfaces the drill drives."""

    def __init__(self, out: Path):
        self.out = out
        self.server_port = int(os.environ.get("HONUA_SERVER_PORT") or free_port())
        self.db_port = int(os.environ.get("HONUA_DR_DB_PORT") or free_port())
        self.redis_port = int(os.environ.get("HONUA_DR_REDIS_PORT") or free_port())
        manifest_bytes = MANIFEST.read_bytes()
        manifest = yaml.safe_load(manifest_bytes)
        server = manifest["components"]["honua-server"]
        self.candidate_digest = sha256_hex(manifest_bytes)
        self.release = manifest["platformRelease"]
        self.server_sha = server["sha"]
        self.image_digest = server["digest"]
        self.image = f"{server['image'].split(':')[0]}@{server['digest']}"
        recovery = manifest.get("disasterRecovery")
        if not isinstance(recovery, dict):
            raise SystemExit("platform-manifest.yaml declares no disasterRecovery inventory; the drill "
                             "has no candidate-owned substrate set to execute against")
        self.topology = recovery["topology"]
        self.objectives = recovery["objectives"]
        self.substrates = [name for name, enabled in recovery["substrates"].items() if enabled]
        self.env = {
            **os.environ,
            "HONUA_SERVER_IMAGE": self.image,
            "HONUA_DR_PROJECT": PROJECT,
            "HONUA_SERVER_PORT": str(self.server_port),
            "HONUA_DR_DB_PORT": str(self.db_port),
            "HONUA_DR_REDIS_PORT": str(self.redis_port),
            "HONUA_ADMIN_PASSWORD": ADMIN_KEY,
        }

    # ---- compose -------------------------------------------------------------------
    def compose(self, *args, check=True, timeout=1200):
        cmd = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args]
        proc = subprocess.run(cmd, check=False, env=self.env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(f"compose failed: {' '.join(args)}\n{proc.stdout.decode('utf-8', 'replace')}")
        return proc.stdout.decode("utf-8", "replace")

    def container_id(self, service: str) -> str:
        return self.compose("ps", "-q", service).strip()

    def volume(self, name: str) -> str:
        return f"{PROJECT}_{name}"

    def volume_exists(self, name: str) -> bool:
        proc = run("docker", "volume", "inspect", self.volume(name), check=False)
        return proc.returncode == 0

    def volume_is_empty(self, name: str, mount: str) -> bool:
        listing = run("docker", "run", "--rm", "-u", "0:0", "-v", f"{self.volume(name)}:{mount}",
                      "--entrypoint", "/bin/sh", self.image, "-c",
                      f"ls -A {mount} | head -5").stdout.decode().strip()
        return listing == ""

    # ---- HTTP product surface ------------------------------------------------------
    def request(self, method: str, path: str, body=None, headers=None, raw=False, timeout=120):
        url = f"http://127.0.0.1:{self.server_port}{path}"
        head = {"X-API-Key": ADMIN_KEY}
        head.update(headers or {})
        data = None
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            else:
                data = json.dumps(body).encode("utf-8")
                head.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, method=method, headers=head)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail[:400]}") from exc
        return payload if raw else json.loads(payload.decode("utf-8"))

    def form_post(self, path: str, fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]):
        boundary = "----honua-dr-drill-" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
        for key, (filename, content, content_type) in files.items():
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n".encode() + content + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        return self.request("POST", path, body=b"".join(chunks),
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    # ---- substrate surfaces --------------------------------------------------------
    def psql(self, sql: str, database: str = "honua") -> str:
        proc = run("docker", "exec", "-i", self.container_id("db"), "psql", "-X", "-v", "ON_ERROR_STOP=1",
                   "-U", "honua", "-d", database, "-Atc", sql)
        return proc.stdout.decode("utf-8").strip()

    def redis(self) -> Resp:
        return Resp("127.0.0.1", self.redis_port)

    def wait_postgres_ready(self, seconds: int = 180) -> datetime:
        """Readiness after the PostGIS initialization restart, not merely a healthy report."""
        time.sleep(3)
        deadline = time.time() + seconds
        last = ""
        while time.time() < deadline:
            proc = run("docker", "exec", "-i", self.container_id("db"), "pg_isready",
                       "-U", "honua", "-d", "honua", check=False)
            if proc.returncode == 0:
                return now()
            last = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            time.sleep(1)
        raise RuntimeError(f"the restored PostgreSQL never accepted connections: {last}")

    def wait_healthy(self, service: str, seconds: int = 420) -> datetime:
        container = self.container_id(service)
        deadline = time.time() + seconds
        while time.time() < deadline:
            state = run("docker", "inspect", "-f", "{{.State.Health.Status}}", container,
                        check=False).stdout.decode().strip()
            if state == "healthy":
                return now()
            if state == "unhealthy":
                raise RuntimeError(f"{service} reported unhealthy during recovery")
            time.sleep(2)
        raise RuntimeError(f"{service} did not become healthy within {seconds}s")


def canonical(value) -> bytes:
    """Byte form the drill hashes: the observation, serialized deterministically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def observation(state_id: str, payload: bytes, count: int, surface: str) -> dict:
    return {
        "stateId": state_id,
        "sha256": sha256_hex(payload),
        "count": count,
        "runtimeSurface": surface,
        "observedAt": stamp(now()),
    }


# ---- phase 1: seed durable state through the real product surfaces --------------------

def seed_state(stack: Stack) -> None:
    log("seeding durable state through the product surfaces")
    # postgresql: file import is the product's own PostgreSQL write path — it creates the physical
    # table and its rows inside the server, with no SQL from the drill — and publication makes those
    # exact rows readable back through the serving protocols.
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-156.33, 20.75]},
                 "properties": {"name": "dr-imported-alpha"}},
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-156.45, 20.88]},
                 "properties": {"name": "dr-imported-bravo"}}]
    payload = json.dumps({"type": "FeatureCollection", "features": features}).encode("utf-8")
    imported = stack.form_post("/api/v1/admin/import/upload",
                               {"TableName": IMPORT_TABLE, "TargetSrid": "4326"},
                               {"file": ("dr-drill.geojson", payload, "application/geo+json")})
    if not imported.get("success") or imported.get("featureCount") != len(features):
        raise RuntimeError(f"import did not land the drill's features: {imported}")
    physical_table = imported["physicalTableName"]

    connection = stack.request("POST", "/api/v1/admin/connections/", {
        "name": "dr-pg", "host": "db", "port": 5432, "databaseName": "honua",
        "username": "honua", "password": "honua", "provider": "PostGIS",
        "sslRequired": False, "sslMode": "Disable"})["data"]["connectionId"]
    # Geometry column and primary key are deliberately left to server-side introspection: the
    # import owns the physical shape, and asserting a column name here would only couple the drill
    # to an import detail it does not control.
    stack.request("POST", f"/api/v1/admin/connections/{connection}/layers", {
        "schema": imported.get("schema", "honua_data"), "table": physical_table,
        "layerName": SERVICE, "serviceName": SERVICE,
        "geometryType": "Point", "srid": 4326, "enabled": True})

    # transactional-outbox + redis: a transactional insert through OGC API Features writes the
    # outbox row in the same transaction as the feature and publishes the durable change event.
    #
    # NOTE: on this candidate that inserted row is NOT readable back through the serving protocols
    # of a published (source-backed) layer — the transaction writes into the managed feature store
    # while reads resolve the published source table. That is a honua-server defect, reported with
    # this drill; it is called out here so nobody later reads the `postgresql` evidence below as a
    # claim about OGC transactional inserts. The outbox row and the change event ARE durably
    # written, which is what the two substrates below are about, and the managed store the insert
    # lands in is inside the same PostgreSQL backup either way.
    # Geometry only: the attribute shape belongs to the import, and the substrates this insert
    # drives care that a mutation was committed, not what it carried.
    created = stack.request("POST", f"/ogc/features/collections/{SERVICE}/items", {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-156.5, 20.9]},
        "properties": {}},
        headers={"Content-Type": "application/geo+json"})
    object_id = int(created["id"])

    # object-storage: attachment bytes land in the configured file store, referenced from the row.
    attachment = stack.form_post(
        f"/rest/services/{SERVICE}/FeatureServer/{LAYER_ID}/{object_id}/addAttachment",
        {"f": "json"}, {"attachment": ("dr-object-storage.txt", b"honua dr drill object bytes\n", "text/plain")})
    attachment_id = attachment["addAttachmentResult"]["objectId"]

    # job-queue: a real geoprocessing submission through the durable Redis-backed job runtime.
    submitted = stack.request(
        "POST", f"/rest/services/geoprocessing/GPServer/{GP_TASK}/submitJob",
        body=f"f=json&wkb={urllib.parse.quote(GP_WKB)}&srid=4326&distance=0.01".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if "jobId" not in submitted:
        raise RuntimeError(f"geoprocessing submission refused: {submitted}")
    job_id = submitted["jobId"]
    deadline = time.time() + 300
    while time.time() < deadline:
        status = stack.request("GET", f"/rest/services/geoprocessing/GPServer/{GP_TASK}/jobs/{job_id}?f=json")
        if status.get("jobStatus") in ("esriJobSucceeded", "esriJobFailed"):
            break
        time.sleep(2)
    else:
        raise RuntimeError("geoprocessing job never reached a terminal state")
    if status["jobStatus"] != "esriJobSucceeded":
        raise RuntimeError(f"geoprocessing job did not succeed: {status}")

    # workflow-cursors: publishing a package version to a Schedule target persists the compiled
    # workflow definition in the durable orchestration store. Without it the server refuses the
    # publication outright, so a successful publish IS the substrate being composed.
    stack.request("POST", "/api/v1/console/workflow-packages", {
        "packageId": PACKAGE_ID, "name": "DR drill durable workflow",
        "description": "Durable scheduled workflow the full-platform DR drill recovers",
        "graph": {"schemaVersion": "workflow-package.v1", "edges": [], "nodes": [
            {"nodeId": "buffer", "nodeTypeId": f"process:{GP_TASK}",
             "parameters": {"wkb": GP_WKB, "srid": "4326", "distance": "0.01"}}]}})
    version = stack.request("POST", f"/api/v1/console/workflow-packages/{PACKAGE_ID}/versions", {})["data"]["version"]
    publication = stack.request(
        "POST", f"/api/v1/console/workflow-packages/{PACKAGE_ID}/versions/{version}/publish",
        {"publicationId": PUBLICATION_ID, "target": "Schedule", "enabled": True,
         "schedule": {"cronExpression": "0 3 * * *", "timeZone": "UTC", "enabled": True}})["data"]
    stack.state = {
        "objectId": object_id,
        "attachmentId": attachment_id,
        "jobId": job_id,
        "workflowDefinitionId": publication["workflowDefinitionId"],
    }
    log(f"seeded: feature {object_id}, attachment {attachment_id}, job {job_id}, "
        f"workflow {stack.state['workflowDefinitionId']}")


# ---- phase 2: observe each substrate through its real runtime surface -----------------

def observe(stack: Stack, substrate: str) -> dict:
    state = stack.state
    if substrate == "postgresql":
        items = stack.request("GET", f"/ogc/features/collections/{SERVICE}/items?limit=1000")
        rows = sorted(
            [{"id": str(feature["id"]),
              "properties": feature.get("properties") or {},
              "geometry": feature["geometry"]} for feature in items["features"]],
            key=lambda row: row["id"])
        # The rows the drill itself wrote have to be in what is observed; a catalog that merely
        # answers is not proof that written state came back. The import owns the attribute names,
        # so match on the values the drill supplied rather than on a column name.
        payload = canonical(rows)
        served = payload.decode("utf-8")
        missing = [marker for marker in ("dr-imported-alpha", "dr-imported-bravo") if marker not in served]
        if missing:
            raise RuntimeError(f"imported features absent from the collection: {missing}")
        return observation("dr-sentinel-features", payload, len(rows),
                           f"GET /ogc/features/collections/{SERVICE}/items")
    if substrate == "transactional-outbox":
        # The outbox is a PostgreSQL table the dispatcher owns; SQL is its substrate surface.
        # Dispatch timestamps are deliberately excluded: the recovery claim is that the durable
        # rows survive, not that a restored dispatcher re-stamps them identically.
        raw = stack.psql(
            "SELECT string_agg(outbox_id || '|' || service_id || '|' || layer_id || '|' || object_id "
            "|| '|' || operation || '|' || protocol || '|' || event_id || '|' || status, E'\\n' "
            "ORDER BY outbox_id) || '#' || count(*) FROM honua.feature_change_outbox")
        count = int(raw.rsplit("#", 1)[1])
        return observation("feature-change-outbox", raw.encode("utf-8"), count,
                           "SELECT over honua.feature_change_outbox")
    if substrate == "redis":
        replay = stack.request("GET", "/api/v1/admin/feature-events/replay?limit=1000")
        events = sorted(
            [{"eventId": event["EventId"], "cursor": event["Cursor"], "serviceId": event["ServiceId"],
              "layerId": event["LayerId"], "objectId": event["ObjectId"],
              "operation": event["Operation"], "protocol": event["Protocol"]}
             for event in replay["Events"]],
            key=lambda event: event["cursor"])
        return observation("feature-change-event-store", canonical(events), len(events),
                           "GET /api/v1/admin/feature-events/replay")
    if substrate == "object-storage":
        payload = stack.request(
            "GET", f"/rest/services/{SERVICE}/FeatureServer/{LAYER_ID}/{state['objectId']}"
                   f"/attachments/{state['attachmentId']}", raw=True)
        return observation(f"attachment-{state['attachmentId']}", payload, 1,
                           f"GET /rest/services/{SERVICE}/FeatureServer/{LAYER_ID}/"
                           f"{state['objectId']}/attachments/{state['attachmentId']}")
    if substrate == "job-queue":
        status = stack.request(
            "GET", f"/rest/services/geoprocessing/GPServer/{GP_TASK}/jobs/{state['jobId']}?f=json")
        record = {"jobId": status["jobId"], "jobStatus": status["jobStatus"],
                  "results": sorted(status.get("results", {}))}
        return observation(state["jobId"], canonical(record), 1,
                           f"GET /rest/services/geoprocessing/GPServer/{GP_TASK}/jobs/{{jobId}}")
    if substrate == "workflow-cursors":
        with stack.redis() as client:
            definition = client.call("GET", f"orchestration:def:{state['workflowDefinitionId']}")
            registered = client.call("SISMEMBER", "orchestration:def:all", state["workflowDefinitionId"])
        if not definition or not registered:
            raise RuntimeError("the durable workflow definition is absent from the orchestration store")
        return observation(state["workflowDefinitionId"], definition, 1,
                           "GET orchestration:def:{workflowDefinitionId} on the durable workflow store")
    raise RuntimeError(f"no drill surface is implemented for substrate {substrate!r}")


def instance_identities(stack: Stack) -> dict[str, str]:
    """Boot identity of the runtime that owns each substrate's primary state."""
    with stack.redis() as client:
        info = client.call("INFO", "server").decode("utf-8", "replace")
    run_id = next(line.split(":", 1)[1].strip() for line in info.splitlines() if line.startswith("run_id:"))
    system_identifier = stack.psql("SELECT system_identifier FROM pg_control_system()")
    server_container = stack.container_id("server")
    return {
        "postgresql": f"postgresql-cluster:{system_identifier}",
        "transactional-outbox": f"postgresql-cluster:{system_identifier}",
        "redis": f"redis-run:{run_id}",
        "job-queue": f"redis-run:{run_id}",
        "workflow-cursors": f"redis-run:{run_id}",
        "object-storage": f"server-container:{server_container}",
    }


# ---- phase 3: back every enabled substrate up through its own supported path ----------

def redis_slice_export(stack: Stack, patterns: list[str]) -> bytes:
    """DUMP every key the slice owns, with its TTL, in key order.

    DUMP/RESTORE is Redis's own logical backup path and is what the restore replays, so the
    bytes hashed here are exactly the bytes the recovery consumed.
    """
    entries = []
    with stack.redis() as client:
        keys: list[bytes] = []
        for pattern in patterns:
            keys.extend(client.scan_keys(pattern))
        for key in sorted(set(keys)):
            payload = client.call("DUMP", key)
            if payload is None:
                continue
            ttl = client.call("PTTL", key)
            entries.append({"key": base64.b64encode(key).decode(),
                            "ttl": max(int(ttl), 0) if int(ttl) > 0 else 0,
                            "payload": base64.b64encode(payload).decode()})
    return canonical({"schema": "honua.dr-redis-slice/v1", "entries": entries})


def volume_helper(stack: Stack, volume: str, mount: str, script: str, stdin=None, capture=True):
    """Run a shell snippet against a named volume using the candidate's own image.

    Using the pinned server image keeps the drill's tool surface inside the certified artifact
    set instead of pulling an unrelated utility image into the recovery path.
    """
    args = ["docker", "run", "--rm", "-i", "-u", "0:0", "-v", f"{stack.volume(volume)}:{mount}",
            "--entrypoint", "/bin/sh", stack.image, "-c", script]
    return run(*args, stdin=stdin, capture=capture)


def take_backups(stack: Stack) -> tuple[dict[str, dict], datetime]:
    log("capturing substrate backups behind the write boundary")
    backups: dict[str, dict] = {}
    db = stack.container_id("db")

    if "postgresql" in stack.substrates:
        dump = run("docker", "exec", "-i", db, "pg_dump", "-U", "honua", "-d", "honua",
                   "--format=custom", "--compress=9", "--no-owner").stdout
        path = stack.out / "postgresql.dump"
        path.write_bytes(dump)
        backups["postgresql"] = {"id": path.name, "sha256": sha256_hex(dump), "path": path}
    if "transactional-outbox" in stack.substrates:
        dump = run("docker", "exec", "-i", db, "pg_dump", "-U", "honua", "-d", "honua",
                   "--format=custom", "--no-owner", "-t", "honua.feature_change_outbox").stdout
        path = stack.out / "transactional-outbox.dump"
        path.write_bytes(dump)
        backups["transactional-outbox"] = {"id": path.name, "sha256": sha256_hex(dump), "path": path}
    for substrate, patterns in REDIS_SLICES.items():
        if substrate in stack.substrates:
            payload = redis_slice_export(stack, patterns)
            path = stack.out / f"{substrate}.redis-slice.json"
            path.write_bytes(payload)
            backups[substrate] = {"id": path.name, "sha256": sha256_hex(payload), "path": path}
    if "object-storage" in stack.substrates:
        archive = volume_helper(stack, "honua_storage", "/storage",
                                "tar -C /storage -cf - . 2>/dev/null").stdout
        path = stack.out / "object-storage.tar"
        path.write_bytes(archive)
        backups["object-storage"] = {"id": path.name, "sha256": sha256_hex(archive), "path": path}

    missing = set(stack.substrates) - backups.keys()
    if missing:
        raise RuntimeError("no backup path is implemented for enabled substrate(s): " + ", ".join(sorted(missing)))
    for substrate, backup in backups.items():
        log(f"  {substrate}: {backup['id']} {backup['sha256']} ({backup['path'].stat().st_size} bytes)")
    return backups, now()


# ---- phase 4: destroy the primary state ----------------------------------------------

def destroy(stack: Stack) -> datetime:
    log("destroying primary state (containers and every named volume)")
    volumes = ["db_data", "redis_data", "honua_storage"]
    stack.compose("down", "-v", "--remove-orphans")
    moment = now()
    surviving = [name for name in volumes if stack.volume_exists(name)]
    if surviving:
        raise RuntimeError("primary state survived destruction: " + ", ".join(surviving))
    return moment


# ---- phase 5: restore into freshly created, verified-empty stores ---------------------

def create_clean_volumes(stack: Stack) -> None:
    for name, mount in (("db_data", "/var/lib/postgresql/data"), ("redis_data", "/data"),
                        ("honua_storage", "/storage")):
        run("docker", "volume", "create", stack.volume(name))
        if not stack.volume_is_empty(name, mount):
            raise RuntimeError(f"{name} was not created as a clean store")


def restore(stack: Stack, backups: dict[str, dict]) -> dict[str, datetime]:
    ready: dict[str, datetime] = {}
    create_clean_volumes(stack)

    if "object-storage" in stack.substrates:
        volume_helper(stack, "honua_storage", "/storage",
                      "tar -C /storage -xf - && chown -R 1001:1001 /storage",
                      stdin=backups["object-storage"]["path"].read_bytes())

    log("starting the clean stores")
    stack.compose("up", "-d", "--wait", "db", "redis")
    # The PostGIS entrypoint performs one controlled restart after its healthcheck can first
    # succeed, so a healthy report inside that initialization window is not a usable cluster.
    stack.wait_healthy("db")
    db_ready = stack.wait_postgres_ready()
    redis_ready = stack.wait_healthy("redis")

    if "postgresql" in stack.substrates:
        db = stack.container_id("db")
        existing = stack.psql("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'honua_data'")
        if existing != "0":
            raise RuntimeError("the recreated database was not a clean store")
        # --clean/--if-exists because a freshly initialized PostGIS cluster already carries the
        # extension's own template schemas; the drill's cleanliness assertion above is what proves
        # the store held no candidate data.
        run("docker", "exec", "-i", db, "pg_restore", "-U", "honua", "-d", "honua", "--no-owner",
            "--clean", "--if-exists", "--exit-on-error", stdin=backups["postgresql"]["path"].read_bytes())
        ready["postgresql"] = now()
        ready["transactional-outbox"] = ready["postgresql"]
        # The outbox slice is its own artifact and must be usable on its own, not merely a copy
        # carried inside the cluster dump: replay it into a scratch database and compare.
        if "transactional-outbox" in stack.substrates:
            verify_outbox_backup(stack, backups["transactional-outbox"])

    with stack.redis() as client:
        if int(client.call("DBSIZE")) != 0:
            raise RuntimeError("the recreated Redis was not a clean store")
        for substrate in REDIS_SLICES:
            if substrate not in stack.substrates:
                continue
            entries = json.loads(backups[substrate]["path"].read_text())["entries"]
            for entry in entries:
                client.call("RESTORE", base64.b64decode(entry["key"]), entry["ttl"],
                            base64.b64decode(entry["payload"]))
            ready[substrate] = now()
            log(f"  restored {len(entries)} {substrate} keys from {backups[substrate]['id']}")

    log("restarting the application over the restored stores")
    stack.compose("up", "-d", "--wait", "server")
    server_ready = stack.wait_healthy("server")
    ready.setdefault("object-storage", server_ready)
    for substrate in stack.substrates:
        ready.setdefault(substrate, max(db_ready, redis_ready, server_ready))
    return ready


def verify_outbox_backup(stack: Stack, backup: dict) -> None:
    db = stack.container_id("db")
    run("docker", "exec", "-i", db, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "honua",
        "-d", "postgres", "-c", "DROP DATABASE IF EXISTS honua_outbox_verify")
    run("docker", "exec", "-i", db, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "honua",
        "-d", "postgres", "-c", "CREATE DATABASE honua_outbox_verify")
    run("docker", "exec", "-i", db, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "honua",
        "-d", "honua_outbox_verify", "-c", "CREATE SCHEMA honua")
    run("docker", "exec", "-i", db, "pg_restore", "-U", "honua", "-d", "honua_outbox_verify",
        "--no-owner", "--clean", "--if-exists", "--exit-on-error", stdin=backup["path"].read_bytes())
    scratch = stack.psql("SELECT count(*) FROM honua.feature_change_outbox", database="honua_outbox_verify")
    restored = stack.psql("SELECT count(*) FROM honua.feature_change_outbox")
    if scratch != restored or scratch == "0":
        raise RuntimeError(f"the outbox backup artifact does not stand on its own "
                           f"({scratch} rows restored vs {restored} in the recovered cluster)")


# ---- phase 6: assemble, measure and sign ---------------------------------------------

def sign(out: Path) -> None:
    key = Path(os.environ.get("HONUA_DR_SIGNING_KEY", out / "receipt-key.pem"))
    if not key.exists():
        run("openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key))
        key.chmod(0o600)
    run("openssl", "pkey", "-in", str(key), "-pubout", "-out", str(out / "receipt.pub.pem"))
    run("openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(key),
        "-in", str(out / "receipt.json"), "-out", str(out / "receipt.json.sig"))
    run("openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(out / "receipt.pub.pem"),
        "-in", str(out / "receipt.json"), "-sigfile", str(out / "receipt.json.sig"))
    names = ["receipt.json", "receipt.json.sig", "receipt.pub.pem"]
    digest = "\n".join(f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}" for name in names)
    (out / "SHA256SUMS").write_text(digest + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path(os.environ.get("HONUA_DR_OUTPUT",
                                                    ROOT / "artifacts" / "dr-drill-full-platform")))
    parser.add_argument("--keep", action="store_true", help="leave the recovered stack running")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    stack = Stack(args.output)
    log(f"candidate {stack.release} server {stack.server_sha[:12]} image {stack.image_digest[:19]}")
    log(f"topology {stack.topology}; enabled substrates: {', '.join(sorted(stack.substrates))}")

    started = now()
    try:
        stack.compose("up", "-d", "--wait")
        stack.wait_healthy("server")
        seed_state(stack)
        last_write = now()

        written = {substrate: observe(stack, substrate) for substrate in sorted(stack.substrates)}
        before = instance_identities(stack)

        # Quiesce the application writers so every substrate backup describes one recovery point.
        stack.compose("stop", "server")
        backups, backup_completed = take_backups(stack)
        # RPO: the age of the recovery point at the moment the backup set closed — the window of
        # accepted writes a failure immediately after the backup would have cost.
        rpo_ms = (backup_completed - last_write).total_seconds() * 1000.0

        stopped = destroy(stack)
        ready = restore(stack, backups)
        after = instance_identities(stack)

        read = {substrate: observe(stack, substrate) for substrate in sorted(stack.substrates)}
    finally:
        if not args.keep:
            stack.compose("down", "-v", "--remove-orphans", check=False)

    substrates = {}
    for substrate in sorted(stack.substrates):
        if before[substrate] == after[substrate]:
            raise SystemExit(f"{substrate}: the runtime instance identity did not change across recovery; "
                             "a graceful restart with intact primary state is not recovery evidence")
        substrates[substrate] = {
            "backup": {
                "id": backups[substrate]["id"],
                "sha256": backups[substrate]["sha256"],
                "primaryStateDestroyed": True,
                "restoredIntoCleanStore": True,
            },
            "restartRecovery": {
                "writtenBeforeRestart": written[substrate],
                "readAfterRestart": read[substrate],
                "stoppedAt": stamp(stopped),
                "readyAt": stamp(ready[substrate]),
                "instanceBefore": before[substrate],
                "instanceAfter": after[substrate],
            },
        }

    completed = now()
    # RTO is the outage the receipt itself records: earliest destruction to the latest moment the
    # restored state was readable again through a runtime surface. Never a declared constant.
    recovered = max(datetime.fromisoformat(entry["readAfterRestart"]["observedAt"].replace("Z", "+00:00"))
                    for entry in (value["restartRecovery"] for value in substrates.values()))
    rto_ms = (recovered - stopped).total_seconds() * 1000.0

    receipt = {
        "schema": "honua.dr-drill-receipt/v2",
        "scope": "full-platform",
        "status": "pass",
        "topology": stack.topology,
        "candidateLockDigest": stack.candidate_digest,
        "candidate": {
            "platformRelease": stack.release,
            "serverSha": stack.server_sha,
            "imageDigest": stack.image_digest,
            "configuration": {"path": "platform-manifest.yaml", "sha256": stack.candidate_digest},
        },
        "startedAt": stamp(started),
        "completedAt": stamp(completed),
        "measurements": {
            "rpoMs": round(rpo_ms, 3),
            "rtoMs": round(rto_ms, 3),
            "rpoDefinition": "last accepted durable write to completion of the substrate backup set",
            "rtoDefinition": "earliest primary-state destruction to the latest post-restart read "
                             "through a runtime surface",
        },
        "substrates": substrates,
        "producer": {
            "workflow": ".github/workflows/dr-drill-local-docker.yml",
            "repository": os.environ.get("GITHUB_REPOSITORY", "honua-io/honua-release"),
            "ref": os.environ.get("GITHUB_REF", "local"),
            "runUrl": (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                       f"{os.environ['GITHUB_RUN_ID']}") if os.environ.get("GITHUB_RUN_ID") else "local",
        },
    }
    (args.output / "receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sign(args.output)

    validator = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "validate_dr_receipt.py"),
         "--candidate", str(MANIFEST), "--receipt", str(args.output / "receipt.json")],
        check=False)
    if validator.returncode != 0:
        return 1
    log(f"full-platform DR PASS receipt={args.output / 'receipt.json'} "
        f"rpoMs={rpo_ms:.0f} rtoMs={rto_ms:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
