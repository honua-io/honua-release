"""Run-owned TLS edge for documentation that uses production gRPC channel credentials.

Only the public test CA enters a documentation session. The candidate's h2c listener,
the published SDK example, and machine-wide trust stores remain unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import time
import uuid

# Same immutable support image as certification/console-read-approve/run.py.
CADDY_IMAGE = "caddy@sha256:df7f1c2fb114453b951de51a98efc010db1655a92c2e86be6706714e2417a78d"
ADDRESS = "localhost:8443"
CA_ENV = "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"
CADDYFILE = """{
    admin off
    auto_https disable_redirects
    skip_install_trust
}
https://localhost:8443 {
    bind 127.0.0.1
    tls internal
    reverse_proxy h2c://127.0.0.1:8081
}
"""

# Executed in the documentation network, with the public CA mounted read-only.
# A connection error is never evidence that an untrusted certificate was refused.
TLS_PROBE = r'''
import hashlib, json, socket, ssl, sys, time
ca, port = sys.argv[1], int(sys.argv[2])
def connect(context):
    context.set_alpn_protocols(["h2"])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname="localhost") as tls:
            if tls.selected_alpn_protocol() != "h2":
                raise RuntimeError("TLS endpoint did not negotiate HTTP/2")
            return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
for attempt in range(30):
    try:
        connect(ssl.create_default_context())
    except ssl.SSLCertVerificationError:
        break
    except ConnectionRefusedError:
        if attempt == 29:
            raise
        time.sleep(1)
    else:
        raise RuntimeError("Untrusted fixture CA was unexpectedly accepted")
leaf = connect(ssl.create_default_context(cafile=ca))
print(json.dumps({"untrustedCaRefused": True, "trustedTls": True,
                  "alpn": "h2", "leafCertificateSha256": leaf}))
'''


def required(variables: dict) -> bool:
    return any("{candidate.grpcTlsAddress}" in item.get("value", "")
               for item in variables.get("substitute", {}).values())


class GrpcTlsFixture:
    def __init__(self, docker, work: Path, network: str, python_image: str, run_id: str):
        self.docker = docker
        self.work = work
        self.network = network
        self.python_image = python_image
        self.name = f"execdocs-grpc-tls-{run_id}-{uuid.uuid4().hex[:12]}"
        self.probe_name = self.name + "-probe"
        self.ca = work / "root.crt"
        self.evidence: dict = {}
        self.started = False

    def start(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        config = self.work / "Caddyfile"
        config.write_text(CADDYFILE, encoding="utf-8")
        os.chmod(config, 0o644)
        # Private CA material lives only in this sidecar's memory-backed filesystem.
        self.started = True
        try:
            self.docker("run", "-d", "--name", self.name, "--network", self.network,
                        "--label", "honua.execdocs.fixture=grpc-tls", "--tmpfs", "/data",
                        "--tmpfs", "/config", "-v", f"{config}:/etc/caddy/Caddyfile:ro",
                        CADDY_IMAGE, check=True, timeout=300)
            for attempt in range(30):
                # Docker's archive/cp API cannot read tmpfs mounts. Read exactly the
                # public certificate inside the container; never export /data or keys.
                exported = self.docker("exec", self.name, "cat", "/data/caddy/pki/authorities/local/root.crt")
                if exported.returncode == 0:
                    pem = exported.stdout.strip()
                    if not pem.startswith("-----BEGIN CERTIFICATE-----") or not pem.endswith("-----END CERTIFICATE-----") or "PRIVATE KEY" in pem:
                        raise RuntimeError("gRPC TLS fixture returned an invalid public CA certificate")
                    ssl.PEM_cert_to_DER_cert(pem)
                    self.ca.write_text(pem + "\n", encoding="ascii")
                    break
                if attempt == 29:
                    raise RuntimeError("gRPC TLS fixture did not produce its public test CA: " + exported.stderr[-1000:])
                time.sleep(1)
            os.chmod(self.ca, 0o644)
            probe = self.docker("run", "--rm", "--name", self.probe_name, "--network", self.network,
                                "-v", f"{self.ca}:/fixture/root.crt:ro", self.python_image,
                                "python3", "-c", TLS_PROBE, "/fixture/root.crt", "8443",
                                check=True, timeout=90)
            result = json.loads(probe.stdout)
            if result.get("untrustedCaRefused") is not True or result.get("trustedTls") is not True or result.get("alpn") != "h2":
                raise RuntimeError("gRPC TLS preflight did not prove both trust controls and HTTP/2")
            self.evidence = {"proxyImage": CADDY_IMAGE, "address": ADDRESS,
                             "upstream": "h2c://127.0.0.1:8081", "trustScope": "documentation session only",
                             "publicCaSha256": hashlib.sha256(self.ca.read_bytes()).hexdigest(), **result}
            (self.work / "tls-evidence.json").write_text(json.dumps(self.evidence, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            diagnostics = {}
            for label, command in (("state", ("inspect", "--format", "{{json .State}}", self.name)),
                                   ("logs", ("logs", "--tail", "30", self.name))):
                try:
                    result = self.docker(*command)
                    diagnostics[label] = (result.stdout + result.stderr)[-4000:]
                except Exception as diagnostic_error:
                    diagnostics[label] = str(diagnostic_error)
            try:
                self.close()
            except Exception as cleanup_error:
                diagnostics["cleanup"] = str(cleanup_error)
            raise RuntimeError(f"gRPC TLS fixture failed: {exc}; diagnostics={json.dumps(diagnostics)}") from exc

    def session_env(self, state: Path) -> dict[str, str]:
        if not self.evidence:
            raise RuntimeError("gRPC TLS fixture has not passed its trust preflight")
        target = state / "grpc-test-root.crt"
        shutil.copyfile(self.ca, target)
        os.chmod(target, 0o644)
        return {CA_ENV: str(target)}

    def close(self) -> None:
        if self.started:
            errors = []
            for name in (self.probe_name, self.name):
                try:
                    result = self.docker("rm", "-f", "-v", name)
                    if result.returncode and "No such container" not in result.stderr:
                        errors.append(f"{name}: {result.stderr}")
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
            self.started = bool(errors)
            if errors:
                raise RuntimeError("gRPC TLS fixture cleanup failed: " + "; ".join(errors))
