#!/usr/bin/env python3
"""Focused Console read/approve receipt for honua-server#3365.

Boots one isolated stack from immutable images: PostGIS, Redis, a real OIDC IdP
(Keycloak), the honua-server candidate, and the manifest-pinned Console image.
It then proves the `admin:read` + `admin:approve` API-key recipe against the
server, and has the pinned Console, signed in as a SEPARATE human operator
through the server-issued operator bearer, witness the exact proposals that
the scoped key resolved.

Credentials are generated per run, held in memory or in a private temporary
directory, and never written to the receipt. The receipt refuses to be written
if any generated secret appears in it or in a captured Console page.

This is not the sealed terminal-journey receipt: the Console producer's sealed
Studio handoff input has no producer on Studio main, so that input is recorded
as not exercised rather than simulated.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

POSTGIS_IMAGE = "postgis/postgis@sha256:60f6ad1d21ea86a67d47780b9a0d1e1d200500f62b19293fa834d0dea80b8677"
REDIS_IMAGE = "redis@sha256:ccd6aa8d45ff3f033d6fa15b8cc1a50579f65c89f38cf9bb607a954c4f2128ed"
CADDY_IMAGE = "caddy@sha256:df7f1c2fb114453b951de51a98efc010db1655a92c2e86be6706714e2417a78d"
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak@sha256:09a381c715ab0b111835b70f2905955274843a219c6f27efb348e4d9f4086858"
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
READ_APPROVE = ["admin:read", "admin:approve"]
READ_ONLY = ["admin:read"]
INCOMPLETE = object()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def manifest_component(manifest, name):
    """Read one component block from a platform manifest without a YAML dependency."""
    block, inside = {}, False
    for line in Path(manifest).read_text().splitlines():
        if re.match(rf"^  {re.escape(name)}:\s*$", line):
            inside = True
            continue
        if inside and re.match(r"^  \S", line):
            break
        match = re.match(r'^    (\w+):\s*"([^"]*)"', line) if inside else None
        if match:
            block[match.group(1)] = match.group(2)
    return block


def image_revision(image):
    return run("docker", "image", "inspect", image, "--format",
               '{{index .Config.Labels "org.opencontainers.image.revision"}}')


def free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def prove(args):
    server_image = f"ghcr.io/honua-io/honua-server@{args.server_digest}"
    console = manifest_component(args.manifest, "honua-console")
    require(console.get("digest", "").startswith("sha256:"), "Manifest does not pin a Console digest")
    console_image = f"ghcr.io/honua-io/honua-console@{console['digest']}"
    require(image_revision(server_image) == args.server_revision,
            "Server image source revision differs from the expected nightly")
    console_revision = run("docker", "image", "inspect", console_image, "--format",
                           "{{range .Config.Env}}{{println .}}{{end}}")
    console_revision = next((line.split("=", 1)[1] for line in console_revision.splitlines()
                             if line.startswith("HONUA_CONSOLE_COMMIT_SHA=")), "")
    require(console_revision == console["sha"],
            "Console image commit differs from the manifest-pinned Console sha")

    images = {name: {"image": image, "imageId": run("docker", "image", "inspect", image, "--format", "{{.Id}}")}
              for name, image in [("server", server_image), ("console", console_image), ("postgis", POSTGIS_IMAGE),
                                  ("redis", REDIS_IMAGE), ("keycloak", KEYCLOAK_IMAGE),
                                  ("edge", CADDY_IMAGE)]}
    secrets_in_play = []

    def secret(size=32):
        value = secrets.token_hex(size)
        secrets_in_play.append(value)
        return value

    admin_password = "Aa1!" + secret()
    secrets_in_play.append(admin_password)
    db_password, master_key, bearer_key = secret(), secret(), secret()
    client_secret, edge_secret, operator_password = secret(), secret(), secret()
    project = "console-read-approve-" + uuid.uuid4().hex[:10]
    console_port, server_port, idp_port = free_port(), free_port(), free_port()
    console_origin = f"http://127.0.0.1:{console_port}"
    idp_origin = f"https://host.docker.internal:{idp_port}"

    receipt = {
        "schemaVersion": "honua.console.focused-read-approve/v1",
        "issue": "honua-io/honua-server#3365",
        "consoleIssue": "honua-io/honua-console#351",
        "observedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "server": {"image": server_image, "tag": args.server_tag, "sourceRevision": args.server_revision},
        "console": {"image": console_image, "tag": console.get("image"), "sourceRevision": console["sha"],
                    "manifestPinned": True, "mode": "witness", "sharedAdminKeyConfigured": False},
        "images": images,
        "operator": {"identityProvider": "keycloak (real OIDC, PKCE, TLS)", "credential": "server-issued operator bearer",
                     "edge": "caddy reverse proxy injecting the operator identity; Console port unpublished",
                     "realmRoles": ["admin"], "tenantClaim": "tenant_id=public",
                     "distinctFromApiKeys": True},
        "checks": {},
        "notExercised": {
            "sealedTerminalHandoff": (
                "The canonical Console receipt producer (npm run receipt:console) requires the sealed paused "
                "Studio handoff honua.studio.real-model-ai-arc-handoff/v1 and a zero-to-map checkpoint paused at "
                "console-approval. The handoff producer (release:real-model-ai-arc) exists only on unmerged "
                "honua-studio#45, and the terminal journey is still blocked (honua-release#122/#123), so no "
                "sealed handoff exists to consume. It was not simulated."),
        },
    }

    scratch = Path.home() / ".cache" / "honua-console-read-approve"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=scratch) as directory:
        work = Path(directory)
        os.chmod(work, 0o700)
        certs = work / "certs"
        certs.mkdir()
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
            "-keyout", str(certs / "kc.key"), "-out", str(certs / "kc.crt"), "-subj", "/CN=host.docker.internal",
            "-addext", "subjectAltName=DNS:host.docker.internal,DNS:localhost,IP:127.0.0.1",
            stderr=subprocess.DEVNULL)
        for path in certs.iterdir():
            os.chmod(path, 0o644)
        realm_dir = work / "realm"
        realm_dir.mkdir()
        realm = {
            "realm": "honua", "enabled": True, "sslRequired": "none", "registrationAllowed": False,
            "roles": {"realm": [{"name": "admin"}, {"name": "user"}]},
            "users": [{"username": "release-operator", "enabled": True, "email": "release-operator@honua.invalid",
                       "emailVerified": True, "firstName": "Release", "lastName": "Operator",
                       "credentials": [{"type": "password", "value": operator_password, "temporary": False}],
                       "realmRoles": ["admin", "user"]}],
            "clients": [{
                "clientId": "honua-console-bff", "enabled": True, "protocol": "openid-connect",
                "publicClient": False, "secret": client_secret, "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False,
                "redirectUris": [f"{console_origin}/admin/auth/callback"], "webOrigins": [console_origin],
                "attributes": {"pkce.code.challenge.method": "S256"},
                "protocolMappers": [{
                    "name": "realm-roles-flat", "protocol": "openid-connect",
                    "protocolMapper": "oidc-usermodel-realm-role-mapper", "consentRequired": False,
                    "config": {"claim.name": "roles", "jsonType.label": "String", "multivalued": "true",
                               "id.token.claim": "true", "access.token.claim": "true",
                               "userinfo.token.claim": "true"}},
                    # The operator acts in the deployment's default tenant, the same tenant the scoped keys use.
                    {"name": "tenant", "protocol": "openid-connect", "protocolMapper": "oidc-hardcoded-claim-mapper",
                     "consentRequired": False,
                     "config": {"claim.name": "tenant_id", "claim.value": "public", "jsonType.label": "String",
                                "id.token.claim": "true", "access.token.claim": "true",
                                "userinfo.token.claim": "true"}}],
            }],
        }
        (realm_dir / "honua-realm.json").write_text(json.dumps(realm))
        os.chmod(realm_dir / "honua-realm.json", 0o644)
        os.chmod(realm_dir, 0o755)
        os.chmod(certs, 0o755)

        server_env = {
            "ASPNETCORE_ENVIRONMENT": "Development",
            "ASPNETCORE_URLS": "http://+:8080",
            "ConnectionStrings__DefaultConnection":
                f"Host=postgres;Database=honua;Username=honua;Password={db_password}",
            "ConnectionStrings__Redis": "redis:6379",
            "HONUA_ADMIN_PASSWORD": admin_password,
            "Security__ConnectionEncryption__MasterKey": master_key,
            "HostValidation__AllowedHosts__0": "127.0.0.1",
            "HostValidation__AllowedHosts__1": "server",
            "Licensing__DevGrantEdition": "Pro",
            "RateLimiting__Enabled": "false",
            "PUBLIC_BASE_URL": console_origin,
            "Oidc__Enabled": "true",
            "Oidc__Generic__Enabled": "true",
            "Oidc__Generic__DisplayName": "Release operator IdP",
            "Oidc__Generic__Authority": f"{idp_origin}/realms/honua",
            "Oidc__Generic__ClientId": "honua-console-bff",
            "Oidc__Generic__ClientSecret": client_secret,
            "Authentication__OperatorBearer__Enabled": "true",
            "Authentication__OperatorBearer__SigningKey": bearer_key,
            "SSL_CERT_FILE": "/certs/kc.crt",
        }
        compose = {"services": {
            "postgres": {"image": POSTGIS_IMAGE, "environment": {
                "POSTGRES_DB": "honua", "POSTGRES_USER": "honua", "POSTGRES_PASSWORD": db_password},
                "healthcheck": {"test": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U honua -d honua"],
                                "interval": "2s", "retries": 30}},
            "redis": {"image": REDIS_IMAGE, "command": ["redis-server", "--appendonly", "yes"]},
            "keycloak": {
                "image": KEYCLOAK_IMAGE, "command": ["start", "--import-realm"],
                "environment": {
                    "KC_HOSTNAME": idp_origin, "KC_HTTP_ENABLED": "false", "KC_HTTPS_PORT": str(idp_port),
                    "KC_HTTPS_CERTIFICATE_FILE": "/opt/keycloak/certs/kc.crt",
                    "KC_HTTPS_CERTIFICATE_KEY_FILE": "/opt/keycloak/certs/kc.key", "KC_HEALTH_ENABLED": "true"},
                "volumes": [f"{certs}:/opt/keycloak/certs:ro", f"{realm_dir}:/opt/keycloak/data/import:ro"],
                "ports": [f"127.0.0.1:{idp_port}:{idp_port}"],
                # The IdP is addressed by one issuer URL from the server container and the browser.
                "networks": {"default": {"aliases": ["host.docker.internal"]}}},
            "server": {"image": server_image, "ports": [f"127.0.0.1:{server_port}:8080"],
                       "environment": server_env, "volumes": [f"{certs}:/certs:ro"], "restart": "on-failure:5",
                       "depends_on": {"postgres": {"condition": "service_healthy"},
                                      "keycloak": {"condition": "service_started"}}},
            # The trusted edge: the only published path to the Console. It replaces any client-supplied
            # identity headers with the operator identity and never forwards an access token, so the
            # Console can reach honua-server only through the bearer it exchanges for the operator.
            "edge": {"image": CADDY_IMAGE, "ports": [f"127.0.0.1:{console_port}:8080"],
                     "environment": {"EDGE_SECRET": edge_secret},
                     "volumes": [f"{work / 'Caddyfile'}:/etc/caddy/Caddyfile:ro"],
                     "depends_on": ["console"]},
            "console": {"image": console_image,
                        "environment": {
                            "ASPNETCORE_ENVIRONMENT": "Production",
                            "HONUA_SERVER_BASE_URL": "http://server:8080",
                            "HONUA_CONSOLE_MODE": "witness",
                            "Honua__Console__Auth__Mode": "EdgeForwarded",
                            "Honua__Console__Auth__EdgeForwarded__Enabled": "true",
                            "Honua__Console__Auth__EdgeForwarded__SharedSecret": edge_secret},
                        "depends_on": ["server"]},
        }}
        compose_path = work / "compose.json"
        (work / "Caddyfile").write_text("""{
	admin off
	auto_https off
}
:8080 {
	reverse_proxy console:8080 {
		header_up -X-Forwarded-Access-Token
		header_up X-Forwarded-User release-operator
		header_up X-Forwarded-Email release-operator@honua.invalid
		header_up X-Honua-Edge-Auth {env.EDGE_SECRET}
	}
}
""")
        os.chmod(work / "Caddyfile", 0o644)

        def dc(*arguments):
            return run("docker", "compose", "-p", project, "-f", str(compose_path), *arguments)

        def up():
            compose_path.write_text(json.dumps(compose))
            os.chmod(compose_path, 0o600)
            dc("up", "-d")

        endpoint = f"http://127.0.0.1:{server_port}"

        def call(path, method="GET", body=None, key=admin_password):
            headers = {"Content-Type": "application/json"}
            if key is not None:
                headers["X-API-Key"] = key
            request = urllib.request.Request(endpoint + path, method=method, headers=headers,
                                             data=None if body is None else json.dumps(body).encode())
            try:
                response = urllib.request.urlopen(request, timeout=30)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                try:
                    raw = response.read().decode(errors="replace")
                except http.client.IncompleteRead:
                    # A streamed body that ends early still carries its authorization status.
                    return response.code, INCOMPLETE
                try:
                    parsed = json.loads(raw) if raw[:1] in "{[" else raw
                except ValueError:
                    parsed = raw
                return response.code, parsed

        def wait(url_check, what, seconds=240):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    if url_check():
                        return
                except (OSError, TimeoutError, urllib.error.URLError):
                    # Connection refusals are expected while containers start.
                    pass
                time.sleep(2)
            raise RuntimeError(f"{what} did not become ready")

        def server_ready():
            wait(lambda: call("/healthz/ready", key=None)[0] == 200, "Server")

        def expect(path, status, **kwargs):
            observed, body = call(path, **kwargs)
            # Never echo bodies: key-mint responses carry credential material.
            require(observed == status, f"{kwargs.get('method', 'GET')} {path}: expected {status}, got {observed}")
            return body

        try:
            up()
            server_ready()
            wait(lambda: urllib.request.urlopen(f"{console_origin}/version.json", timeout=5).status == 200,
                 "Console")
            version = expect("/api/v1/admin/version", 200)
            version = version.get("data", version) if isinstance(version, dict) else {}
            receipt["server"]["reportedVersion"] = version.get("version")
            receipt["server"]["reportedSourceRevision"] = version.get("sourceRevision")
            if version.get("sourceRevision"):
                require(version["sourceRevision"].startswith(args.server_revision[:7]),
                        "Running server reports a different source revision")

            # 1. Mint both keys through the Admin API and read back their exact effective grants.
            keys = {}
            for name, grants in [("readApprove", READ_APPROVE), ("readOnly", READ_ONLY)]:
                created = expect("/api/v1/admin/api-keys", 201, method="POST",
                                 body={"name": f"console-{name}-{project}", "permissions": grants})["data"]
                secrets_in_play.append(created["key"])
                effective = expect(f"/api/v1/admin/api-keys/{created['apiKey']['id']}/effective-permissions",
                                   200, key=created["key"])["data"]
                require(sorted(effective["permissions"]) == sorted(grants), f"{name} effective grants differ")
                require(effective["status"] == "active" and effective["canAuthenticate"] is True,
                        f"{name} key is not active")
                keys[name] = {"key": created["key"], "id": created["apiKey"]["id"], "grants": grants,
                              "effective": effective["permissions"]}
            receipt["checks"]["mintKeysThroughAdminApi"] = {
                "status": "passed",
                "keys": {name: {"grants": value["grants"], "effectivePermissions": value["effective"]}
                         for name, value in keys.items()}}

            # 2. Every parameterless admin GET the running image documents: the scoped key must be
            #    authorized exactly where the full admin is, and never 401/403.
            spec = INCOMPLETE
            for _ in range(5):
                spec = expect("/api/v1/admin/openapi.json", 200)
                if spec is not INCOMPLETE:
                    break
            require(isinstance(spec, dict), "Admin OpenAPI document could not be read completely")
            base = "/api/v1/admin"
            get_paths = sorted(path for path, operations in spec.get("paths", {}).items()
                               if "get" in operations and "{" not in path)
            require(len(get_paths) >= 50, "Admin OpenAPI document lists too few parameterless GET routes")
            rows, mismatches = [], []
            for path in get_paths:
                full = path if path.startswith("/api/") else base + path
                admin_status, _ = call(full)
                approve_status, approve_body = call(full, key=keys["readApprove"]["key"])
                read_status, _ = call(full, key=keys["readOnly"]["key"])
                row = {"path": full, "admin": admin_status, "readApprove": approve_status, "readOnly": read_status}
                if approve_body is INCOMPLETE:
                    row["readApproveBodyEndedEarly"] = True
                rows.append(row)
                if approve_status in (401, 403) or (approve_status != admin_status and admin_status not in (401, 403)):
                    mismatches.append(full)
            distribution = {}
            for row in rows:
                distribution[str(row["readApprove"])] = distribution.get(str(row["readApprove"]), 0) + 1
            receipt["checks"]["adminGetsAuthorizedForReadApproveKey"] = {
                "status": "passed" if not mismatches else "failed",
                "routes": len(rows), "readApproveStatusCounts": dict(sorted(distribution.items())),
                "authorizationDenials": sum(1 for row in rows if row["readApprove"] in (401, 403)),
                "statusDiffersFromFullAdmin": mismatches,
                "nonOkRoutes": [row for row in rows if row["readApprove"] != 200]}
            # Recorded, not raised: a GET authorization defect must not hide the remaining criteria.

            # 3. The unrelated write stays denied for both scoped keys.
            write_denials = {}
            for name in keys:
                denied = expect("/api/v1/admin/services/x/access-policy", 403, method="PUT",
                                body={"allowAnonymous": True}, key=keys[name]["key"])
                write_denials[name] = {"status": 403, "detail": denied.get("detail") if isinstance(denied, dict) else None}
            receipt["checks"]["unrelatedAccessPolicyWriteDenied"] = {"status": "passed", "responses": write_denials}

            # 4. Pause three real mutations for approval (the same guardrail path #4736 proved).
            drafts = {}
            for label in ["approve", "reject", "pending"]:
                draft = expect("/api/v1/studio/package-drafts", 201, method="POST", body={
                    "packageKey": f"console-witness-{label}", "workspaceId": project,
                    "envelope": {"family": "query", "schemaVersion": "1.0",
                                 "format": "studio_query_package.v1", "body": {"where": "population > 42"}},
                })["data"]
                drafts[label] = expect("/api/v1/studio/package-drafts/" + draft["draftId"], 200)["data"]
            server_env["Guardrails__Overrides__StudioDraftMutation"] = "RequiresApproval"
            up()
            server_ready()
            proposals = {}
            for label, draft in drafts.items():
                handle = expect("/api/v1/studio/package-drafts/" + draft["draftId"], 202, method="DELETE")["data"]
                proposal = expect("/api/v1/admin/proposals/" + handle["proposalId"], 200,
                                  key=keys["readApprove"]["key"])
                require(proposal["status"] == "AwaitingApproval", f"{label} deletion did not pause for approval")
                proposals[label] = {"proposalId": handle["proposalId"],
                                    "operationInstanceId": handle.get("operationInstanceId"),
                                    "draftId": draft["draftId"], "before": proposal}

            # 5. admin:read alone cannot decide; the denial names the missing grant and changes nothing.
            read_only_denials = {}
            for label, decision in [("approve", "approve"), ("reject", "reject")]:
                path = f"/api/v1/admin/proposals/{proposals[label]['proposalId']}"
                body = {"reason": "Console read/approve receipt"} if decision == "reject" else None
                denied = expect(f"{path}/{decision}", 403, method="POST", body=body, key=keys["readOnly"]["key"])
                detail = denied.get("detail", "") if isinstance(denied, dict) else ""
                require("admin:approve" in detail, "read-only denial does not name admin:approve")
                require(expect(path, 200, key=keys["readApprove"]["key"]) == proposals[label]["before"],
                        "read-only decision changed the proposal")
                require(expect("/api/v1/studio/package-drafts/" + proposals[label]["draftId"], 200)["data"]
                        == drafts[label], "read-only decision changed the draft")
                read_only_denials[decision] = {"status": 403, "detail": detail,
                                               "type": denied.get("type") if isinstance(denied, dict) else None}
            receipt["checks"]["readOnlyKeyDecisionDenied"] = {"status": "passed", "responses": read_only_denials}

            # 6. admin:read + admin:approve decides; the effect is real and attributed to the key.
            decisions = {}
            for label, decision, expected in [("approve", "approve", "Succeeded"), ("reject", "reject", "Rejected")]:
                path = f"/api/v1/admin/proposals/{proposals[label]['proposalId']}"
                body = {"reason": "Console read/approve receipt"} if decision == "reject" else None
                code, result = call(f"{path}/{decision}", method="POST", body=body, key=keys["readApprove"]["key"])
                require(code == 200, f"read/approve key {decision}: expected 200, got {code}")
                final = expect(path, 200, key=keys["readApprove"]["key"])
                require(final["status"] == expected, f"{decision} did not persist {expected}")
                require(final.get("resolvedBy") == keys["readApprove"]["id"], f"{decision} actor is not the scoped key")
                draft_status = call("/api/v1/studio/package-drafts/" + proposals[label]["draftId"])[0]
                require(draft_status == (404 if decision == "approve" else 200), f"{decision} draft effect differs")
                decisions[label] = {"decision": decision, "httpStatus": code, "proposalId": final["proposalId"],
                                    "status": final["status"], "resolvedByScopedKey": True,
                                    "executionOperationId": final.get("executionOperationId"),
                                    "draftAfter": draft_status}
            receipt["checks"]["readApproveKeyDecides"] = {"status": "passed", "decisions": decisions}

            # 7. The pinned Console, signed in as a separate operator over the server-issued bearer, witnesses
            #    the exact proposals. It has no admin key; nothing it renders can come from the scoped keys.
            browser_out = work / "browser"
            browser_out.mkdir()
            browser_env = dict(os.environ)
            browser_env.update({
                "RECEIPT_CONSOLE_ORIGIN": console_origin,
                "RECEIPT_OPERATOR_USER": "release-operator",
                "RECEIPT_OPERATOR_PASSWORD": operator_password,
                "RECEIPT_IDP_HOST": f"host.docker.internal:{idp_port}",
                "RECEIPT_PROPOSALS": json.dumps({label: value["proposalId"] for label, value in proposals.items()}),
                "RECEIPT_OUT": str(browser_out),
                "RECEIPT_PLAYWRIGHT": args.playwright,
            })
            completed = subprocess.run(["node", str(HERE / "browser.mjs")], env=browser_env, text=True,
                                       capture_output=True, timeout=900)
            browser = json.loads((browser_out / "observations.json").read_text()) \
                if (browser_out / "observations.json").exists() else {}
            captured = "".join(path.read_text(errors="replace") for path in browser_out.glob("*.html"))
            leaked = [index for index, value in enumerate(secrets_in_play) if value and value in captured]
            require(not leaked, "a generated credential appeared in a captured Console page")
            receipt["checks"]["consoleOperatorWitness"] = {
                "status": "passed" if completed.returncode == 0 else "failed",
                "observations": browser.get("observations"),
                "error": browser.get("error"),
                "capturedPagesSha256": hashlib.sha256(captured.encode()).hexdigest(),
                "credentialMaterialInCapturedPages": False,
            }
            require(completed.returncode == 0, f"Console witness failed: {browser.get('error') or completed.stderr[-400:]}")
            witnessed = browser["observations"]["proposals"]
            for label, expected in [("approve", "Succeeded"), ("reject", "Rejected"), ("pending", "AwaitingApproval")]:
                require(witnessed[label]["proposalId"] == proposals[label]["proposalId"],
                        f"Console witnessed a different {label} proposal")
                require(witnessed[label]["status"] == expected, f"Console shows {label} as {witnessed[label]['status']}")
            failed = sorted(name for name, check in receipt["checks"].items() if check["status"] != "passed")
            receipt["status"] = "passed" if not failed else "failed"
            receipt["failedChecks"] = failed
            require(not failed, f"receipt checks failed: {failed}")
        except Exception as error:  # noqa: BLE001 - the failure is recorded, then re-raised.
            receipt["status"] = "failed"
            receipt["error"] = str(error)
            raise
        finally:
            serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
            leaked = [index for index, value in enumerate(secrets_in_play) if value and value in serialized]
            if leaked:
                serialized = json.dumps({"status": "refused", "error": "credential material reached the receipt"})
            Path(args.output).write_text(serialized)
            if not args.keep:
                subprocess.run(["docker", "compose", "-p", project, "-f", str(compose_path), "down", "-v"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server-digest", required=True)
    parser.add_argument("--server-revision", required=True)
    parser.add_argument("--server-tag", required=True)
    parser.add_argument("--playwright", required=True,
                        help="absolute path to an installed @playwright/test package directory")
    parser.add_argument("--manifest", default=str(REPO / "platform-manifest.yaml"),
                        help="platform manifest that pins the Console image")
    parser.add_argument("--output", required=True)
    parser.add_argument("--keep", action="store_true", help="leave the stack running for diagnosis")
    args = parser.parse_args()
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", args.server_digest), "server digest must be sha256:<64 hex>")
    require(re.fullmatch(r"[0-9a-f]{40}", args.server_revision), "server revision must be a full sha")
    prove(args)
    print(f"Console read/approve receipt passed: {args.output}")


if __name__ == "__main__":
    main()
