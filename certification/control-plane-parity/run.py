#!/usr/bin/env python3
"""Opt-in engineering replay. Never replaces a release gate or edits release pins."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

from cleanup import cleanup
import requests
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROJECT = "honua-sdk1424-fixed"
BASE = "http://127.0.0.1:19816"


def check_identity(image: str, source: str) -> None:
    if not re.fullmatch(r"ghcr\.io/honua-io/honua-server(?::[a-zA-Z0-9_.-]+)?@sha256:[a-f0-9]{64}", image):
        raise ValueError("An immutable Honua server registry digest is required")
    if not re.fullmatch(r"[a-f0-9]{40}", source):
        raise ValueError("An exact server source commit is required")


def selected_clients(manifest: dict) -> dict:
    """Select only the independently reviewed published pair; never resolve latest."""
    expected = {"honua-sdk-js": "@honua/sdk-js", "honua-mcp-server": "@honua/mcp-server"}
    selected = {}
    for key, name in expected.items():
        pin = manifest["clientArtifacts"][key]
        if (pin["package"] != name or pin["ecosystem"] != "npm"
                or pin["repository"] != "honua-io/honua-sdk-js"
                or pin["publicationState"] != "published"
                or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", pin["version"])
                or not re.fullmatch(r"[a-f0-9]{40}", pin["sourceSha"])
                or not re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", pin["integrity"])):
            raise ValueError(f"{key}: an exact published reviewed identity is required")
        selected[name] = {field: pin[field] for field in ("version", "sourceSha", "integrity", "filename")}
    sdk = selected["@honua/sdk-js"]
    component = manifest["components"]["honua-sdk-js"]
    if component["version"] != sdk["version"] or component["sha"] != sdk["sourceSha"]:
        raise ValueError("SDK component and artifact identities differ")
    return selected


def sanitize(text: str, private_values: list[str]) -> str:
    for value in sorted(set(private_values), key=len, reverse=True):
        if value:
            text = text.replace(value, "<private-run-value>")
    text = re.sub(r"https?[^\s\"<>]*X-Amz-[^\s\"<>]*", "<private-presigned-source>", text, flags=re.I)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<private-bearer>", text)
    return text


def make_idp_mounts_readable(run: Path) -> None:
    # The private parent stays 0700 on the host. These two directories become
    # mount roots inside Keycloak, whose uid differs from the hosted runner's.
    # The renderer already makes only its throwaway certificate/realm files 0644.
    for name in ("certs", "import"):
        (run / name).chmod(0o755)


def main() -> int:
    os.umask(0o077)
    image, source = os.environ["HONUA_SERVER_IMAGE"], os.environ["HONUA_SERVER_SOURCE"]
    check_identity(image, source)
    clients = selected_clients(yaml.safe_load((ROOT / "platform-manifest.yaml").read_text()))
    final_route = os.environ.get("HONUA_PARITY_FINAL_ROUTE", "false") == "true"
    terminal_draft = os.environ.get("HONUA_PARITY_TERMINAL_DRAFT", "false") == "true"
    run = Path(os.environ["HONUA_PARITY_RUN"]).resolve()
    run.mkdir(mode=0o700, parents=True, exist_ok=False)
    out = Path(os.environ["HONUA_PARITY_OUT"]).resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifact_credentials = {key: os.environ[key] for key in ("GH_TOKEN", "GITHUB_TOKEN", "RELEASE_GH_TOKEN") if key in os.environ}
    env = {key: value for key, value in os.environ.items() if key not in artifact_credentials}
    passwords = {user: secrets.token_hex(24) for user in ["admin-a", "viewer-a", "admin-b", "admin-read-a", "approver-a"]}
    env.update({
        "S4_RUN_DIR": str(run), "S4_IDP_HOSTNAME": "host.docker.internal", "S4_IDP_PORT": "19808",
        "S4_SERVER_PORT": "19816", "S4_CONSOLE_ORIGIN": BASE, "S4_OPERATOR_USER": "admin-a",
        "S4_OPERATOR_PASSWORD": passwords["admin-a"], "S4_AUDIT_CHAIN_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "S4_ADMIN_KEY": "RunA9!" + secrets.token_hex(24),
        **{name: secrets.token_hex(32) for name in ["S4_CLIENT_SECRET", "S4_OPERATOR_BEARER_KEY", "S4_MASTER_KEY", "S4_DB_PASSWORD", "S4_KEYRING_PASSWORD", "S4_IDP_ADMIN_PASSWORD"]},
        "HONUA_PARITY_CONSUMER": str(run / "consumer"),
    })
    private = list(passwords.values()) + [v for k, v in env.items() if k.startswith("S4_") and any(x in k for x in ["PASSWORD", "SECRET", "KEY"])]
    # Per-run random fixture secrets: 0600 inside a new 0700 directory, excluded from uploads.
    (run / "credentials.json").write_text(json.dumps(passwords))
    (run / "run.env").write_text("".join(f"{k}={v}\n" for k, v in env.items() if k.startswith("S4_") or k == "HONUA_SERVER_IMAGE"))
    (run / "ownership.json").write_text(json.dumps({"project": PROJECT, "runDirectory": str(run), "bucket": env["HONUA_PARITY_BUCKET"]}))
    report = {"kind": "production-control-plane-engineering-replay", "status": "failed", "serverImage": image, "serverSource": source,
              "selectedClients": clients,
              "finalRouteRequested": final_route,
              "terminalDraftRequested": terminal_draft,
              "releaseSource": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "scope": "Bounded actual SDK/CLI/MCP/Studio command stages, followed by automated distinct-principal terminal approval. Not human approval acceptance, full release certification or manifest promotion.", "stages": []}
    log = (run / "execution.log").open("w")
    compose = ["docker", "compose", "-p", PROJECT, "--env-file", str(run / "run.env"), "-f", str(ROOT / "e2e/harness/compose.console-s4.yml"), "-f", str(run / "production.yml")]
    stores_started = False

    def call(args: list[str], *, cwd: Path = run, timeout: int = 300, extra: dict | None = None) -> None:
        subprocess.run(args, cwd=cwd, env={**env, **(extra or {})}, stdout=log, stderr=log, timeout=timeout, check=True)

    def stage(name: str, action) -> None:
        item = {"name": name, "status": "failed"}
        report["stages"].append(item)
        action()
        item["status"] = "passed"
        print(f"PASS: {name}", flush=True)

    try:
        stage("verify reviewed public artifact bytes and provenance", lambda: call([sys.executable, str(ROOT / "tools/verify_client_artifacts.py"), "--manifest", str(ROOT / "platform-manifest.yaml"), "--ecosystem", "npm"], cwd=ROOT, timeout=300, extra=artifact_credentials))
        call(["bash", str(ROOT / "e2e/harness/keycloak/render.sh"), str(run)])
        make_idp_mounts_readable(run)
        realm_path = run / "import/honua-realm.json"
        realm = json.loads(realm_path.read_text())
        template = realm["users"][0]
        realm["users"] = []
        for user, password in passwords.items():
            item = copy.deepcopy(template)
            item.update(username=user, email=f"{user}@sdk1424.honua.test", realmRoles=["user"] if user == "viewer-a" else ["admin", "user"], attributes={"tenant_id": ["tenant-b" if user == "admin-b" else "tenant-a"]})
            if user == "admin-read-a":
                item["attributes"]["honua_scopes"] = ["honua.mcp.read"]
            item["credentials"][0]["value"] = password
            realm["users"].append(item)
        mapper = realm["clients"][0]["protocolMappers"][0]
        assert mapper["name"] == "fixture-tenant"
        mapper["protocolMapper"] = "oidc-usermodel-attribute-mapper"
        mapper["config"].pop("claim.value")
        mapper["config"]["user.attribute"] = "tenant_id"
        realm["clients"][0]["protocolMappers"].append({"name": "sdk1424-per-user-oauth-ceiling", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-attribute-mapper", "consentRequired": False, "config": {"user.attribute": "honua_scopes", "claim.name": "scp", "jsonType.label": "String", "id.token.claim": "true", "access.token.claim": "true", "userinfo.token.claim": "true"}})
        # The throwaway IdP needs run-generated secrets; its parent is private and import never uploaded.
        realm_path.write_text(json.dumps(realm))
        call(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "keyring.key", "-out", "keyring.crt", "-days", "2", "-subj", "/CN=honua-sdk1424-keyring"])
        call(["openssl", "pkcs12", "-export", "-inkey", "keyring.key", "-in", "keyring.crt", "-out", "keyring.pfx", "-passout", "env:S4_KEYRING_PASSWORD"])
        (run / "keyring.pfx").chmod(0o644)
        shutil.copy(HERE / "production.yml", run / "production.yml")
        for path in HERE.glob("*.py"):
            if path.name != "run.py":
                shutil.copy(path, run / path.name)
        for path in HERE.glob("*.mjs"):
            shutil.copy(path, run / path.name)
        for source_name, target in [("map-envelope.json", "published-map-envelope.json"), ("map-envelope-provenance.json", "published-map-envelope-provenance.json")]:
            shutil.copy(HERE / source_name, run / target)
        stage("pull immutable server", lambda: call(["docker", "pull", image], timeout=600))
        inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
        assert inspected["Config"]["Labels"]["org.opencontainers.image.revision"] == source, "Image OCI source mismatch"
        assert any(x.endswith("@" + image.split("@", 1)[1]) for x in inspected["RepoDigests"]), "Pulled image digest mismatch"
        report["imageId"] = inspected["Id"]
        # Compose can create containers and then fail while waiting for readiness.
        stores_started = True
        stage("start persistent stores and IdP", lambda: call([*compose, "up", "-d", "--wait", "--wait-timeout", "300", "db", "redis", "keycloak"], timeout=600))
        stage("real IdP discovery readiness", lambda: call(["curl", "--fail", "--silent", "--show-error", "--cacert", str(run / "certs/kc.crt"), "--resolve", "host.docker.internal:19808:127.0.0.1", "--noproxy", "*", "--retry", "60", "--retry-all-errors", "--retry-delay", "2", "--max-time", "5", "https://host.docker.internal:19808/realms/honua/.well-known/openid-configuration"], timeout=300))
        stage("start Production server", lambda: call([*compose, "up", "-d", "--wait", "--wait-timeout", "300", "server"], timeout=600))
        consumer = run / "consumer"
        consumer.mkdir()
        (consumer / "package.json").write_text(json.dumps({"private": True, "type": "module", "dependencies": {name: pin["version"] for name, pin in clients.items()}}))
        (consumer / "expected-clients.json").write_text(json.dumps(clients, indent=2))
        for name in ("verify-client.mjs", "client-identity.mjs"):
            shutil.copy(HERE / name, consumer / name)
        stage("install public clients with default peer resolution", lambda: call(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org"], cwd=consumer, timeout=300))
        stage("verify actual package and shared module identity", lambda: call(["node", "verify-client.mjs"], cwd=consumer))
        if final_route:
            stage("admit published SDK and MCP typed intent without server calls", lambda: call(["node", "admit-publication-route.mjs"]))
        if terminal_draft:
            stage("admit published terminal draft command by dry run", lambda: call(["node", "admit-terminal-draft.mjs"]))
        stage("real OIDC principals", lambda: call([sys.executable, "authenticate.py"]))
        token = json.loads((run / "admin-a-bearer.json").read_text())["accessToken"]
        integrity = requests.get(BASE + "/api/v1/admin/observability/audit/verify", headers={"Authorization": "Bearer " + token}, timeout=30)
        integrity.raise_for_status()
        assert integrity.json()["verified"] and integrity.json()["unhashedRows"] == 0
        redis = json.loads(subprocess.check_output(["docker", "exec", PROJECT + "-redis-1", "redis-cli", "--json", "CONFIG", "GET", "appendonly", "appendfsync", "maxmemory-policy"]))
        assert redis == {"appendonly": "yes", "appendfsync": "everysec", "maxmemory-policy": "noeviction"}
        runtime = json.loads(subprocess.check_output(["docker", "inspect", PROJECT + "-server-1"]))[0]
        running_env = dict(x.split("=", 1) for x in runtime["Config"]["Env"] if "=" in x)
        assert runtime["Image"] == inspected["Id"] and running_env["ASPNETCORE_ENVIRONMENT"] == "Production"
        (run / "runtime-proof.json").write_text(json.dumps({"sourceCommit": source, "immutableImage": image, "imageId": runtime["Image"], "environment": "Production", "licensingMode": running_env["Licensing__Mode"], "durableRedisConfiguration": redis, "auditIntegrity": integrity.json(), "persistentVolumes": [{"name": x.get("Name"), "destination": x["Destination"]} for x in runtime["Mounts"] if x["Type"] == "volume"], "keyRingCertificateConfigured": bool(running_env.get("Operations__SecretChannel__KeyRingCertificatePath")), "auditChainKeyConfigured": bool(running_env.get("AuditLog__ChainVerification__Key")), "qualification": "engineering replay, not release certification"}, indent=2))
        stage("owned connection", lambda: call([sys.executable, "create-connection.py"]))
        stage("owned GeoJSON object", lambda: call([sys.executable, "create-import-object.py"]))
        stage("connection.test four adapters", lambda: call(["node", "parity-published.mjs"], extra={"PARITY_MODE": "connection", "PARITY_OUTPUT_DIR": str(run / "parity-connection")}))
        call([sys.executable, "import-readback.py", "before"])
        imported = run / "parity-import"
        stage("import.create four adapters", lambda: call(["node", "parity-published.mjs"], extra={"PARITY_MODE": "import", "PARITY_OUTPUT_DIR": str(imported)}))
        stage("owned imported row lineage", lambda: call([sys.executable, "import-readback.py", str(imported)]))
        stage("map-package.publish four adapters", lambda: call(["node", "parity-published.mjs"], extra={"PARITY_MODE": "publish", "PARITY_OUTPUT_DIR": str(run / "parity-publish")}))
        stage("tenant-owned object refusal " + ("four adapters" if terminal_draft else "three adapters"), lambda: call(["node", "studio-tenants-published.mjs"]))
        stage("concurrent background imports and principal isolation", lambda: call([sys.executable, "import-controls.py"], timeout=360))
        stage("terminal proposal polling and distinct authenticated approver", lambda: call(["node", "publication-lifecycle.mjs"], timeout=240))
        final_integrity = requests.get(BASE + "/api/v1/admin/observability/audit/verify", headers={"Authorization": "Bearer " + token}, timeout=30)
        final_integrity.raise_for_status()
        (run / "audit-integrity-final.json").write_text(json.dumps(final_integrity.json(), indent=2))
        assert final_integrity.json()["verified"] and final_integrity.json()["unhashedRows"] == 0
        report["status"] = "bounded-stages-passed"
    except Exception as exc:
        report["failure"] = sanitize(str(exc), private)
        print("Replay failed; sanitized report retains the stage and cause", flush=True)
        if stores_started:
            # Capture bounded diagnostics before teardown. Do not inspect Config/Env;
            # execution.log is sanitized with all run secrets before publication.
            for command in (
                [*compose, "logs", "--no-color", "--tail", "100"],
                ["docker", "inspect", "--format", "{{.Name}} {{json .State}}",
                 *[PROJECT + "-" + name + "-1" for name in ("db", "redis", "keycloak", "server")]],
            ):
                try:
                    subprocess.run(command, env=env, stdout=log, stderr=log, timeout=30, check=False)
                except (OSError, subprocess.TimeoutExpired) as diagnostic_error:
                    log.write(f"Bounded diagnostic failed: {type(diagnostic_error).__name__}\n")
    finally:
        try:
            receipt = cleanup(run, ROOT, env["HONUA_PARITY_BUCKET"])
            (run / "owned-import-cleanup.json").write_text(json.dumps(receipt, indent=2))
            if receipt["status"] == "failed":
                report["status"] = "failed"
        except Exception as exc:
            report["cleanupFailure"] = sanitize(str(exc), private)
            report["status"] = "failed"
        log.close()
        for path in run.glob("*-bearer.json"):
            private.append(json.loads(path.read_text())["accessToken"])
        if (run / "owned-import-url.txt").exists():
            private.append((run / "owned-import-url.txt").read_text().strip())
        allowed = ["principal-proof.json", "runtime-proof.json", "audit-integrity-final.json", "connection-setup.json", "owned-import-object.json", "owned-import-cleanup.json", "imported-feature-provenance.json", "import-controls.json", "consumer/expected-clients.json", "consumer/provenance.json", "consumer/package-lock.json", "execution.log", "published-map-envelope-provenance.json", "publication-route-admission.json", "terminal-draft-admission.json"]
        paths = [run / name for name in allowed] + list(run.glob("parity-*/*.json")) + list(run.glob("studio-tenants-*/*.json"))
        inventory = []
        for path in paths:
            if not path.is_file():
                continue
            target = out / path.relative_to(run)
            target.parent.mkdir(parents=True, exist_ok=True)
            retained = sanitize(path.read_text(), private)
            target.write_text(retained)
            inventory.append({"path": str(target.relative_to(out)), "originalSha256": hashlib.sha256(path.read_bytes()).hexdigest(), "retainedSha256": hashlib.sha256(target.read_bytes()).hexdigest()})
        (out / "artifact-index.json").write_text(json.dumps(inventory, indent=2))
        (out / "report.json").write_text(sanitize(json.dumps(report, indent=2), private))
    return 0 if report["status"] == "bounded-stages-passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
