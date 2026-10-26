#!/usr/bin/env python3
"""Live producer for durable portal group sharing and cross-caller readback (honua-release#691).

Boots the pinned server image twice against one PostgreSQL store (``server`` and ``replica``),
then drives the supported Esri client surface: ``/sharing/rest/generateToken`` for named users,
``/sharing/rest/community`` for groups and ``/sharing/rest/content/items/{id}/share|unshare``.
Every reading is a real response from the candidate:

* ``shared``: a member and a nonmember read the item, search, group content and the underlying
  FeatureServer query after the owner shares the item with a private group;
* ``sharedAfterRestart``: the same reads after the ``server`` container restarts;
* ``sharedOnReplica``: the same reads through the second instance;
* ``unshared``: the reads on ``server`` after the owner unshares through the replica;
* ``unsharedAfterRestart``: the reads after another ``server`` restart.

It also records four refusals: a publisher who does not own the item cannot share it, another
tenant cannot touch the group or grant it, a scoped API key cannot change sharing or become a
portal token, and a member's group read does not grant layer writes. An Everyone/org request
must stay in ``notSharedWith``, and an anonymous caller must not gain visibility.

The output is ``honua.portal-group-sharing-observations/v1`` for ``tools/portal_group_sharing.py``,
plus a transcript of raw responses with tokens and keys redacted. With ``--manifest`` pointing at
a minted candidate lock, the judge binds the document to that exact candidate. Against
``platform-manifest.yaml`` it binds the manifest pin, which is development evidence only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE_COMPOSE = ROOT / "e2e" / "harness" / "compose.candidate.yml"
OVERLAY = HERE / "compose.portal-sharing.yml"
SCHEMA = "honua.portal-group-sharing-observations/v1"
ISSUER, AUDIENCE = "https://portal-sharing.invalid", "portal-sharing"
REFERER = "https://portal-sharing.invalid"
TENANT, FOREIGN_TENANT = "default", "portal-sharing-foreign"
SERVICE, TABLE = "portal691", "portal_sharing_sites"
ROWS = (
    (1, "north", -122.40, 37.80),
    (2, "south", -122.41, 37.70),
    (3, "east", -122.30, 37.75),
)
# Named principals. Usernames are the OIDC subject the candidate projects into the portal token.
PRINCIPALS = {
    "owner": ("portal-owner", ["admin", "publisher"], TENANT),
    "member": ("portal-member", ["org_user"], TENANT),
    "nonmember": ("portal-nonmember", ["org_user"], TENANT),
    "intruder": ("portal-intruder", ["publisher"], TENANT),
    "foreign": ("portal-foreign-admin", ["admin"], FOREIGN_TENANT),
    "foreignMember": ("portal-member", ["org_user"], FOREIGN_TENANT),
}
REFUSED_CODES = {401, 403, 404, 499}


class ProducerError(Exception):
    """The candidate answered in a way that is neither the promised effect nor a refusal."""


# ── pure helpers (unit-tested) ────────────────────────────────────────────────────────────────


def server_pin(manifest_text: str) -> dict[str, str]:
    """components.honua-server sha, image and index digest from a manifest or minted lock."""
    import yaml  # e2e/requirements.txt

    server = ((yaml.safe_load(manifest_text) or {}).get("components") or {}).get("honua-server") or {}
    sha, image, digest = server.get("sha"), server.get("image"), server.get("digest")
    if not (isinstance(sha, str) and len(sha) == 40 and isinstance(image, str) and image
            and isinstance(digest, str) and digest.startswith("sha256:") and len(digest) == 71):
        raise ProducerError("the manifest does not pin honua-server by sha, image and sha256 index digest")
    if "@" in image:
        raise ProducerError("the manifest image must be a tag reference; the digest is bound separately")
    return {"sourceSha": sha, "image": f"{image}@{digest}", "digest": digest}


def mint_bearer(signing_key: str, subject: str, roles: list[str], tenant: str, lifetime: int = 3600) -> str:
    """An HS256 token for the candidate's per-run generic OIDC issuer."""
    def encode(value: dict[str, Any]) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
    now = int(time.time())
    body = encode({"alg": "HS256", "typ": "JWT"}) + b"." + encode({
        "iss": ISSUER, "aud": AUDIENCE, "sub": subject, "preferred_username": subject, "tenant_id": tenant,
        "roles": roles, "iat": now, "nbf": now, "exp": now + lifetime, "jti": secrets.token_hex(16)})
    signature = base64.urlsafe_b64encode(hmac.new(signing_key.encode(), body, hashlib.sha256).digest()).rstrip(b"=")
    return (body + b"." + signature).decode()


def error_code(document: Any) -> int | None:
    error = document.get("error") if isinstance(document, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if type(code) is int else None


def item_visible(document: Any, item: str) -> bool:
    if isinstance(document, dict) and document.get("id") == item and error_code(document) is None:
        return True
    if error_code(document) in REFUSED_CODES:
        return False
    raise ProducerError("item read is neither the item nor a refusal")


def search_visible(document: Any, item: str) -> bool:
    results = document.get("results") if isinstance(document, dict) else None
    if not isinstance(results, list):
        raise ProducerError("search read has no results list")
    return any(isinstance(row, dict) and row.get("id") == item for row in results)


def group_content_visible(document: Any, item: str) -> bool:
    items = document.get("items") if isinstance(document, dict) else None
    if isinstance(items, list) and error_code(document) is None:
        return any(isinstance(row, dict) and row.get("id") == item for row in items)
    if error_code(document) in REFUSED_CODES:
        return False
    raise ProducerError("group content read is neither a listing nor a refusal")


def feature_visible(document: Any, expected: int) -> bool:
    count = document.get("count") if isinstance(document, dict) else None
    if type(count) is int and error_code(document) is None:
        if count != expected:
            raise ProducerError(f"feature count {count} differs from the {expected} stored rows")
        return True
    if error_code(document) in REFUSED_CODES:
        return False
    raise ProducerError("feature read is neither the stored count nor a refusal")


def redact(value: Any, secrets_: list[str]) -> Any:
    text = json.dumps(value)
    for secret in sorted({s for s in secrets_ if s}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return json.loads(text)


def judge_module():
    path = ROOT / "tools" / "portal_group_sharing.py"
    spec = importlib.util.spec_from_file_location("honua_portal_group_sharing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── live client ───────────────────────────────────────────────────────────────────────────────


class Http:
    def __init__(self, transcript: list[dict[str, Any]]):
        self.transcript = transcript

    def call(self, label: str, method: str, url: str, *, form: dict[str, str] | None = None,
             body: Any = None, headers: dict[str, str] | None = None, token: str | None = None) -> tuple[int, Any]:
        merged = {"Accept": "application/json", "Referer": REFERER, **(headers or {})}
        data = None
        if form is not None:
            data = urllib.parse.urlencode({**form, "f": "json"}).encode()
            merged["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            merged["Content-Type"] = "application/json"
        target = url
        if token:
            target += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(token)
        if form is None and method == "GET" and "f=json" not in target:
            target += ("&" if "?" in target else "?") + "f=json"
        request = urllib.request.Request(target, data=data, method=method, headers=merged)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            document = json.loads(raw or b"null")
        except json.JSONDecodeError:
            document = {"nonJson": hashlib.sha256(raw).hexdigest()}
        self.transcript.append({"label": label, "method": method, "path": urllib.parse.urlsplit(url).path,
                                "query": urllib.parse.urlsplit(url).query, "status": status, "response": document})
        return status, document


class Stack:
    def __init__(self, project: str, env: dict[str, str]):
        self.project, self.env = project, env

    def compose(self, *args: str, capture: bool = False, stdin: str | None = None) -> str:
        command = ["docker", "compose", "-p", self.project, "-f", str(BASE_COMPOSE), "-f", str(OVERLAY), *args]
        result = subprocess.run(command, env=self.env, text=True, input=stdin,
                                capture_output=True, timeout=900, check=False)
        if result.returncode != 0:
            raise ProducerError(f"docker compose {args[0]} failed: {result.stderr.strip()[-400:]}")
        return result.stdout if capture else ""

    def container(self, service: str) -> str:
        return self.compose("ps", "-q", service, capture=True).strip()

    def runtime(self, service: str) -> dict[str, str]:
        container = self.container(service)
        out = subprocess.run(["docker", "inspect", "-f", "{{.Id}} {{.State.StartedAt}} {{.Image}}", container],
                             text=True, capture_output=True, check=True).stdout.split()
        return {"container": out[0], "startedAt": out[1], "image": out[2]}

    def image_id(self, reference: str) -> str:
        """Docker's id for the pinned reference; the reference itself carries the index digest."""
        return subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", reference],
                              text=True, capture_output=True, check=True).stdout.strip()

    def sql(self, statement: str) -> str:
        return self.compose("exec", "-T", "db", "psql", "-At", "-v", "ON_ERROR_STOP=1", "-U", "honua", "-d", "honua",
                            "-c", statement, capture=True).strip()


def wait_ready(base: str, deadline: float = 300.0) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(base + "/healthz/ready", timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    raise ProducerError(f"{base} did not become ready")


class Producer:
    def __init__(self, pin: dict[str, str], stack: Stack, bases: dict[str, str], signing_key: str, admin_key: str):
        self.pin, self.stack, self.bases = pin, stack, bases
        self.signing_key, self.admin_key = signing_key, admin_key
        self.transcript: list[dict[str, Any]] = []
        self.http = Http(self.transcript)
        self.secrets: list[str] = [signing_key, admin_key]
        self.item = ""
        self.group = ""

    # identities
    def token(self, who: str, instance: str = "server") -> str:
        subject, roles, tenant = PRINCIPALS[who]
        bearer = mint_bearer(self.signing_key, subject, roles, tenant)
        self.secrets.append(bearer)
        status, document = self.http.call(f"generateToken {who}@{instance}", "POST",
                                          self.bases[instance] + "/sharing/rest/generateToken",
                                          form={"username": subject, "password": bearer, "client": "referer",
                                                "referer": REFERER, "expiration": "60"})
        token = document.get("token") if isinstance(document, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            raise ProducerError(f"generateToken refused the {who} principal on {instance}")
        self.secrets.append(token)
        return token

    def admin(self, label: str, method: str, path: str, body: Any = None) -> Any:
        status, document = self.http.call(label, method, self.bases["server"] + path, body=body,
                                          headers={"X-API-Key": self.admin_key})
        if status >= 300 or (isinstance(document, dict) and document.get("success") is False):
            raise ProducerError(f"harness {label} returned HTTP {status}")
        return document.get("data") if isinstance(document, dict) and "data" in document else document

    def identity(self, instance: str) -> str:
        status, document = self.http.call(f"capabilities {instance}", "GET",
                                          self.bases[instance] + "/api/v1/capabilities/manifest")
        server = document.get("server") if isinstance(document, dict) else None
        revision = server.get("deploymentRevision") if isinstance(server, dict) else None
        if status != 200 or revision != self.pin["sourceSha"]:
            raise ProducerError(f"{instance} does not advertise the pinned source {self.pin['sourceSha']}")
        runtime = self.stack.runtime(instance)
        # The container's image id depends on the image store (config id on the classic store, index
        # digest on containerd), so compare it with the id Docker resolves for the pinned tag@digest.
        if runtime["image"] != self.stack.image_id(self.pin["image"]):
            raise ProducerError(f"{instance} runs {runtime['image']}, not the pinned {self.pin['image']}")
        return f"{instance}:{runtime['container'][:12]}@{runtime['startedAt']}#{revision}"

    # setup
    def seed(self) -> None:
        values = ",".join(f"({gid},'{name}',ST_SetSRID(ST_MakePoint({x},{y}),4326))" for gid, name, x, y in ROWS)
        self.stack.sql(f"DROP TABLE IF EXISTS honua_data.{TABLE}; "
                       f"CREATE TABLE honua_data.{TABLE} (gid integer PRIMARY KEY, name text NOT NULL, "
                       f"geom geometry(Point,4326) NOT NULL); INSERT INTO honua_data.{TABLE} VALUES {values};")
        connection = self.admin("create connection", "POST", "/api/v1/admin/connections/", {
            "name": "portal-sharing", "host": "db", "port": 5432, "databaseName": "honua", "username": "honua",
            "provider": "PostGIS", "password": "honua", "sslRequired": False, "sslMode": "Disable"})
        connection_id = connection.get("connectionId") or connection.get("id")
        published = self.admin("publish layer", "POST", f"/api/v1/admin/connections/{connection_id}/layers", {
            "schema": "honua_data", "table": TABLE, "layerName": "sites", "serviceName": SERVICE,
            "geometryColumn": "geom", "geometryType": "Point", "primaryKey": "gid", "srid": 4326, "enabled": True})
        self.layer = published["layerId"]
        # Private to the publisher role: an org_user reads it only through a group share.
        self.admin("restrict service", "PUT", f"/api/v1/admin/services/{SERVICE}/access-policy",
                   {"allowAnonymous": False, "allowAnonymousWrite": False, "allowedRoles": ["publisher"],
                    "allowedWriteRoles": ["publisher"]})

    def discover_item(self, owner: str) -> str:
        _, document = self.http.call("owner search", "GET", self.bases["server"] + "/sharing/rest/search?q=",
                                     token=owner)
        rows = [row for row in (document.get("results") or []) if isinstance(row, dict)
                and row.get("title") == SERVICE and row.get("type") == "Feature Service"]
        if len(rows) != 1:
            raise ProducerError("the owner cannot find exactly one Feature Service item for the published service")
        return rows[0]["id"]

    # reads
    def view(self, who: str, token: str, instance: str, phase: str) -> dict[str, bool]:
        base = self.bases[instance]
        label = f"{phase} {who}@{instance}"
        _, item = self.http.call(f"{label} item", "GET", f"{base}/sharing/rest/content/items/{self.item}", token=token)
        _, search = self.http.call(f"{label} search", "GET", f"{base}/sharing/rest/search?q=", token=token)
        _, content = self.http.call(f"{label} groupContent", "GET", f"{base}/sharing/rest/content/groups/{self.group}",
                                    token=token)
        _, feature = self.http.call(f"{label} feature", "GET",
                                    f"{base}/rest/services/{SERVICE}/FeatureServer/{self.layer}/query"
                                    "?where=1%3D1&returnCountOnly=true", token=token)
        return {"item": item_visible(item, self.item), "search": search_visible(search, self.item),
                "groupContent": group_content_visible(content, self.item),
                "feature": feature_visible(feature, len(ROWS))}

    def phase(self, phase: str, instance: str) -> dict[str, dict[str, bool]]:
        return {who: self.view(who, self.token(who, instance), instance, phase) for who in ("member", "nonmember")}

    def share(self, label: str, token: str, instance: str, action: str, **form: str) -> tuple[int, Any]:
        return self.http.call(label, "POST", f"{self.bases[instance]}/sharing/rest/content/items/{self.item}/{action}",
                              form=form, token=token)

    def restart(self) -> str:
        before = self.stack.runtime("server")["container"]
        self.stack.compose("restart", "server")
        wait_ready(self.bases["server"])
        if self.stack.runtime("server")["container"] != before:
            raise ProducerError("restart replaced the server container instead of restarting it")
        return self.identity("server")

    # refusals
    def refusals(self, owner: str) -> dict[str, str]:
        result: dict[str, str] = {}

        def unchanged() -> bool:
            member = self.view("member", self.token("member"), "server", "refusal-control")
            return member == {"item": True, "search": True, "groupContent": True, "feature": True}

        status, document = self.share("owner-admin: non-owning publisher share", self.token("intruder"), "server",
                                      "share", groups=self.group)
        result["owner-admin"] = "refused" if error_code(document) == 403 and unchanged() else "granted"

        foreign = self.token("foreign")
        _, added = self.http.call("tenant: foreign addUsers", "POST",
                                  f"{self.bases['server']}/sharing/rest/community/groups/{self.group}/addUsers",
                                  form={"users": PRINCIPALS["nonmember"][0]}, token=foreign)
        _, granted = self.share("tenant: foreign share", foreign, "server", "share", groups=self.group)
        foreign_member = self.token("foreignMember")
        _, read = self.http.call("tenant: member under another tenant", "GET",
                                 f"{self.bases['server']}/rest/services/{SERVICE}/FeatureServer/{self.layer}/query"
                                 "?where=1%3D1&returnCountOnly=true", token=foreign_member)
        tenant_refused = (error_code(added) in REFUSED_CODES
                          and self.group in (granted.get("notSharedWith") or [] if isinstance(granted, dict) else [])
                          and error_code(read) in REFUSED_CODES and unchanged())
        result["tenant"] = "refused" if tenant_refused else "granted"

        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        created = self.admin("scoped-key: mint", "POST", "/api/v1/admin/api-keys/",
                             {"name": "portal-sharing-scoped", "permissions": ["admin:read"], "expiresAt": expires})
        key = created.get("key")
        self.secrets.append(key)
        _, unshared = self.http.call("scoped-key: unshare", "POST",
                                     f"{self.bases['server']}/sharing/rest/content/items/{self.item}/unshare",
                                     form={"groups": self.group}, headers={"X-API-Key": key})
        _, exchanged = self.http.call("scoped-key: generateToken", "POST",
                                      f"{self.bases['server']}/sharing/rest/generateToken",
                                      form={"username": "portal-sharing-scoped", "password": key, "client": "referer",
                                            "referer": REFERER})
        result["scoped-key"] = ("refused" if error_code(unshared) in REFUSED_CODES and error_code(exchanged) is not None
                                and not (isinstance(exchanged, dict) and exchanged.get("token")) and unchanged()
                                else "granted")

        _, edit = self.http.call("layer: member applyEdits", "POST",
                                 f"{self.bases['server']}/rest/services/{SERVICE}/FeatureServer/{self.layer}/applyEdits",
                                 form={"updates": json.dumps([{"attributes": {"gid": 1, "name": "moved"}}])},
                                 token=self.token("member"))
        stored = self.stack.sql(f"SELECT name FROM honua_data.{TABLE} WHERE gid = 1")
        result["layer"] = "refused" if error_code(edit) in REFUSED_CODES and stored == "north" else "granted"
        return result

    def public_unchanged(self) -> bool:
        base = self.bases["server"]
        _, item = self.http.call("anonymous item", "GET", f"{base}/sharing/rest/content/items/{self.item}")
        _, search = self.http.call("anonymous search", "GET", f"{base}/sharing/rest/search?q=")
        _, feature = self.http.call("anonymous feature", "GET", f"{base}/rest/services/{SERVICE}/FeatureServer/"
                                    f"{self.layer}/query?where=1%3D1&returnCountOnly=true")
        return (not item_visible(item, self.item) and not search_visible(search, self.item)
                and not feature_visible(feature, len(ROWS)))

    def run(self) -> dict[str, Any]:
        runtime: dict[str, str] = {"beforeRestart": self.identity("server")}
        self.identity("replica")
        self.seed()
        owner = self.token("owner")
        self.item = self.discover_item(owner)
        _, group = self.http.call("owner createGroup", "POST", self.bases["server"] + "/sharing/rest/community/createGroup",
                                  form={"title": f"portal-sharing-{secrets.token_hex(4)}", "access": "private",
                                        "tags": "release-691"}, token=owner)
        self.group = ((group or {}).get("group") or {}).get("id") or ""
        if not self.group:
            raise ProducerError("the owner could not create a private group")
        _, members = self.http.call("owner addUsers", "POST",
                                    f"{self.bases['server']}/sharing/rest/community/groups/{self.group}/addUsers",
                                    form={"users": PRINCIPALS["member"][0]}, token=owner)
        if PRINCIPALS["member"][0] not in (members.get("members") or []):
            raise ProducerError("the member was not added to the group")
        baseline = self.view("member", self.token("member"), "server", "baseline")
        if any(baseline.values()):
            raise ProducerError("the member can read the item before it is shared, so a share cannot be observed")

        _, shared = self.share("owner share", owner, "server", "share", groups=self.group)
        if self.group not in (((shared or {}).get("sharing") or {}).get("groups") or []):
            raise ProducerError("the share response does not name the group")
        _, broad = self.share("owner share everyone/org", owner, "server", "share", groups=self.group,
                              everyone="true", org="true")
        not_shared_with = sorted(broad.get("notSharedWith") or []) if isinstance(broad, dict) else []
        public_unchanged = self.public_unchanged()

        phases = {"shared": self.phase("shared", "server")}
        policy = self.refusals(owner)
        runtime["afterRestart"] = self.restart()
        phases["sharedAfterRestart"] = self.phase("sharedAfterRestart", "server")
        runtime["replica"] = self.identity("replica")
        phases["sharedOnReplica"] = self.phase("sharedOnReplica", "replica")
        _, unshared = self.share("owner unshare through replica", self.token("owner", "replica"), "replica", "unshare",
                                 groups=self.group)
        if self.group in (((unshared or {}).get("sharing") or {}).get("groups") or []):
            raise ProducerError("the unshare response still names the group")
        phases["unshared"] = self.phase("unshared", "server")
        self.restart()
        phases["unsharedAfterRestart"] = self.phase("unsharedAfterRestart", "server")
        runtime["store"] = "postgresql:" + self.stack.sql("SELECT system_identifier FROM pg_control_system()")
        stored = self.stack.sql(f"SELECT count(*) FROM honua.portal_community_groups WHERE group_id = '{self.group}'")
        if stored != "1":
            raise ProducerError("the accepted group is not in the shared PostgreSQL store")

        return {
            "schema": SCHEMA,
            "candidateSourceSha": self.pin["sourceSha"],
            "candidateImage": self.pin["image"],
            "identities": {"owner": PRINCIPALS["owner"][0], "member": PRINCIPALS["member"][0],
                           "nonmember": PRINCIPALS["nonmember"][0], "group": self.group, "item": self.item},
            "runtime": runtime,
            "notSharedWith": not_shared_with,
            "publicCallerVisibilityUnchanged": public_unchanged,
            "policyRefusals": policy,
            "phases": phases,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml",
                        help="platform manifest, or the minted candidate lock for an exact-candidate receipt")
    parser.add_argument("--output", type=Path, required=True, help="observation document (judge input)")
    parser.add_argument("--transcript", type=Path, required=True, help="raw redacted request/response transcript")
    parser.add_argument("--project", default=f"portal-sharing-{secrets.token_hex(3)}")
    parser.add_argument("--port", type=int, default=18480)
    parser.add_argument("--replica-port", type=int, default=18481)
    parser.add_argument("--keep", action="store_true", help="leave the stack running")
    args = parser.parse_args(argv)

    pin = server_pin(args.manifest.read_text())
    signing_key, admin_key = secrets.token_hex(32), secrets.token_urlsafe(24)
    env = {**os.environ, "HONUA_SERVER_IMAGE": pin["image"], "PORTAL_SHARING_SIGNING_KEY": signing_key,
           "E2E_API_KEY": admin_key, "E2E_SERVER_PORT": str(args.port),
           "PORTAL_SHARING_REPLICA_PORT": str(args.replica_port)}
    stack = Stack(args.project, env)
    bases = {"server": f"http://127.0.0.1:{args.port}", "replica": f"http://127.0.0.1:{args.replica_port}"}
    producer = Producer(pin, stack, bases, signing_key, admin_key)
    document: dict[str, Any] | None = None
    failure = None
    try:
        stack.compose("up", "-d", "--wait")
        document = producer.run()
    except (ProducerError, subprocess.SubprocessError, OSError, KeyError, TypeError) as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        args.transcript.parent.mkdir(parents=True, exist_ok=True)
        args.transcript.write_text(json.dumps(redact({"candidate": pin, "failure": failure,
                                                      "exchanges": producer.transcript}, producer.secrets), indent=2))
        if not args.keep:
            try:
                stack.compose("down", "-v", "--remove-orphans")
            except ProducerError as exc:
                print(f"[portal-sharing] teardown: {exc}", file=sys.stderr)
    if document is None:
        print(f"[portal-sharing] no observation document: {failure}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(redact(document, producer.secrets), indent=2) + "\n")
    verdict = judge_module().qualify(document, candidate={"sourceSha": pin["sourceSha"], "image": pin["image"]})
    print(json.dumps(verdict, indent=2))
    return 0 if verdict.get("status") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
