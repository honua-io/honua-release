"""Engineering-only image rehearsal using unchanged frozen installed client pins.

No candidate manifest is rewritten. A failed frozen proxy remains a failed
rehearsal even when the independently measured HTTP view passes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

import pins
import probes
import run as driver

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-image", required=True)
    parser.add_argument("--server-revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--evidence-uri", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"ghcr\.io/honua-io/honua-server@sha256:[a-f0-9]{64}", args.server_image):
        parser.error("server-image must be an immutable Honua server GHCR digest")
    if not re.fullmatch(r"[a-f0-9]{40}", args.server_revision):
        parser.error("server-revision must be the full source SHA")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_raw = (ROOT / "platform-manifest.yaml").read_bytes()
    manifest = yaml.safe_load(manifest_raw)
    target = json.loads((HERE / "targets/local-docker.json").read_text())
    config = target["compose"]
    project = "honua-discovery-" + uuid.uuid4().hex[:12]
    key = uuid.uuid4().hex + uuid.uuid4().hex
    compose = probes.Compose(str(ROOT / config["file"]), project,
        {config["imageEnv"]: args.server_image, config["portEnv"]: str(config["port"]), target["adminPassword"]["env"]: key})
    receipt = {"schemaVersion": 1, "scope": "setup-discovery-engineering-rehearsal", "qualification": False,
        "status": "fail", "evidenceUri": args.evidence_uri, "observedAt": datetime.now(timezone.utc).isoformat(),
        "manifestSha256": hashlib.sha256(manifest_raw).hexdigest(), "clientArtifacts": pins.receipt_pins(manifest),
        "server": {"requestedImage": args.server_image, "requestedRevision": args.server_revision},
        "limitations": ["Frozen client pins remain unchanged; locally built SDK source is never substituted.",
            "Image override is engineering-only, not a governed candidate cut or image attestation.",
            "No tool execution, live model, approval, saved map or later journey stage is qualified."]}
    try:
        workspace = pins.resolve_client_workspace(manifest, args.output_dir / "clients")
        bindir = None
        if workspace.status == "pass":
            bindir, _, notes = pins.install_executables(workspace, args.output_dir / "install")
            workspace.install_notes = notes
        receipt["clientWorkspace"] = workspace.as_receipt()
        up = compose.up()
        if up.returncode != 0:
            raise ValueError("engineering image stack did not become ready")
        inspected = subprocess.run(["docker", "image", "inspect", args.server_image],
            capture_output=True, text=True, check=True, timeout=30)
        image = json.loads(inspected.stdout)[0]
        labels = image.get("Config", {}).get("Labels") or {}
        if labels.get("org.opencontainers.image.revision") != args.server_revision:
            raise ValueError("image configuration revision does not match the requested source")
        if args.server_image not in image.get("RepoDigests", []):
            raise ValueError("pulled image digest does not match the immutable request")
        receipt["server"].update({"imageId": image["Id"], "repoDigests": image["RepoDigests"],
            "configurationRevision": labels["org.opencontainers.image.revision"]})
        # observe() resolves the key through the existing target environment seam.
        import os
        previous = os.environ.get(target["adminPassword"]["env"])
        os.environ[target["adminPassword"]["env"]] = key
        try:
            observation = driver.observe(target, f"http://127.0.0.1:{config['port']}", workspace,
                bindir, args.server_image, args.server_revision)
        finally:
            if previous is None:
                os.environ.pop(target["adminPassword"]["env"], None)
            else:
                os.environ[target["adminPassword"]["env"]] = previous
        discovery = observation.setup_discovery or {"status": "fail", "error": "server did not reach discovery readiness"}
        (args.output_dir / "setup-discovery.json").write_text(json.dumps(discovery, indent=2) + "\n", encoding="utf-8")
        identity = (observation.capability_manifest or {}).get("server") or (observation.capability_manifest or {}).get("Server") or {}
        revision = identity.get("deploymentRevision") or identity.get("DeploymentRevision")
        receipt["server"]["runtimeRevision"] = revision
        receipt["checks"] = {"ready": observation.ready, "licensingDisabled": observation.licensing_disabled,
            "anonymousAdminRefused": observation.anonymous_admin_status in (401, 403),
            "anonymousApiKeysRefused": observation.anonymous_api_keys_status in (401, 403),
            "runtimeRevisionMatches": revision == args.server_revision,
            "installedClientsVerified": workspace.status == "pass" and bindir is not None,
            "setupDiscoveryParity": discovery["status"] == "pass"}
        receipt["discoveryReceiptSha256"] = hashlib.sha256((args.output_dir / "setup-discovery.json").read_bytes()).hexdigest()
        receipt["status"] = "pass" if all(receipt["checks"].values()) else "fail"
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError):
        # No raw Docker/npm output or exception bodies: they can contain secrets.
        receipt["error"] = "engineering setup or identity verification failed; no qualification claimed"
    finally:
        try:
            receipt["stackRemoved"] = compose.down().returncode == 0
        except (OSError, subprocess.SubprocessError):
            receipt["stackRemoved"] = False
        if not receipt["stackRemoved"]:
            receipt["status"] = "fail"
        (args.output_dir / "rehearsal.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"setup discovery engineering rehearsal: {receipt['status']}; qualification=false")
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
