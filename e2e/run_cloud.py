#!/usr/bin/env python3
"""Cross-cloud parity tier entrypoint (docs/TEST-STRATEGY.md Phase B).

Provisions a real honua-server on a cloud deploy target, runs the canonical (slim) parity set —
including the live capability-manifest check (honua-release#61) — against its endpoint, plus the
canary probe set (STAC/EDR/OData/OGC-Features/tiles/per-service-WMS-WMTS-WCS reachability;
e2e/canary_probes.py), tears it down, and — when a reference endpoint is supplied — asserts parity
with the reference (local docker). Emits a machine-readable gate-report.json the release train
consumes.

Honesty (AGENTS.md): when the target's infra isn't wired (no OIDC creds / no deployable image / no
IaC), the run is BLOCKED, never a fake green. `--require-real` (the train / a real nightly run)
promotes BLOCKED to a hard FAIL so the gate can genuinely fail once infra exists.

Cloud-tier unblock (honua-release#61): the canary probes run here in GENERIC mode — no service/tile
id is configured for a bare terraform-provisioned cell (nothing is seeded there yet), so the
data-dependent probes (render+query smoke, per-service WMS/WMTS/WCS, tile.json) honestly report
BLOCKED rather than a fake pass/fail; the reachability-only probes (health, security headers,
metrics-gated, STAC/EDR/OData/OGC-Features reachability) run for real. A genuine FAIL from any canary
probe (a real break, not just "nothing seeded") reddens the run unconditionally — BLOCKED canary
probes are reported but do not gate, since the ephemeral cloud cells have no seed-data story yet
(distinct from the MCP/Studio/GP/demo `scenarioCoverage` scenarios below. Those scenarios certify on
AWS ECS through the candidate-bound driver added for honua-release#129; serverless and EKS retain the
slim target-parity contract and do not pretend to run the ECS delivery arc).

An UNREACHABLE endpoint is not in that tolerated set (honua-release#128). "Nothing was seeded" is a
missing input; "the deployment never answered" is a missing subject, and a cell that provisioned an
endpoint which then never served fails outright, whatever --require-real says.

  python e2e/run_cloud.py --target aws-serverless [--require-real] [--reference-endpoint URL]

Exit code 0 only when the assembled status is "pass".
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import canary_probes  # noqa: E402
from canonical_checks import (CheckResult, EXTENDED_SCENARIOS, is_endpoint_unreachable,  # noqa: E402
                              make_fetch, run_canonical, run_extended)
from parity import TargetRun, compare  # noqa: E402
from targets import REGISTRY  # noqa: E402
from targets.base import ProvisionError  # noqa: E402

REPORT_PATH = E2E_DIR / "gate-report-cloud.json"

# The cloud/OIDC secrets that gate whether this tier can run at all. When NONE are present the gate
# SELF-SKIPS (status: skipped, why: cloud-creds-unset) so a no-cloud local cut is not failed by it —
# it stays ready to enforce per-RC once an org wires the OIDC role for a labelled candidate.
_CLOUD_CRED_ENV = ("HONUA_AWS_ROLE_ARN", "AWS_ROLE_ARN", "AWS_ACCESS_KEY_ID",
                   "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE")

_READY_ATTEMPTS = 36
_READY_DELAY_SECONDS = 5.0
_AWS_SECRET_ARN = re.compile(
    r"arn:(?:aws|aws-us-gov|aws-cn):secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+"
)

_AI_ARC_REQUIRED_ENV = (
    "HONUA_DEVOPS_DIR",
    "HONUA_SDK_JS_DIR",
    "HONUA_CONSOLE_DIR",
    "HONUA_STUDIO_DIR",
    "HONUA_AI_ARC_FIXTURE_BASE_URL",
    "HONUA_AI_ARC_CONSOLE_ORIGIN",
    "HONUA_AI_PROVIDER",
    "HONUA_AI_MODEL",
    "HONUA_AI_ARC_OUT",
    "HONUA_AI_ARC_PREPARE_CREDENTIAL_SECRET_REF",
    "HONUA_AI_ARC_CONSOLE_TOKEN_SECRET_REF",
    "HONUA_RUN_URL",
)

_DEVOPS_FORBIDDEN_ENV = (
    "HONUA_AI_ARC_PREPARE_CREDENTIAL",
    "HONUA_AI_ARC_CONSOLE_TOKEN",
    "HONUA_ADMIN_KEY",
    "HONUA_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
)

_AI_ARC_CHILD_CREDENTIAL_ENV = (
    "HONUA_AI_ARC_PREPARE_CREDENTIAL",
    "HONUA_AI_ARC_CONSOLE_TOKEN",
    "HONUA_ADMIN_KEY",
    "HONUA_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


def _cloud_creds_present() -> bool:
    return any(os.environ.get(v) for v in _CLOUD_CRED_ENV)


def _mark_provision_attempt() -> None:
    """Record that this cell is about to create real cloud resources.

    The workflow's backstop reaper runs even when the parity step is cancelled mid-apply, where no
    report exists to consult. The marker is what tells it the difference between "nothing was ever
    deployed" and "something may be half-applied and MUST be destroyed".
    """
    marker = os.environ.get("HONUA_CLOUD_PROVISION_MARKER")
    if not marker:
        return
    path = Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _check_dicts(results) -> list[dict]:
    return [{"name": r.name, "status": r.status, "why": r.why, **({"evidence": r.evidence} if r.evidence else {})}
            for r in results]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The only credential-shaped fields written here are validated Secrets Manager ARN references;
    # credential values remain process-only.
    # codeql[py/clear-text-storage-sensitive-data]
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _git_head(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProvisionError(f"AI delivery arc checkout identity unavailable: {path}") from error
    return result.stdout.strip()


def _run_secretless(
    command: list[str], *, env: dict[str, str], label: str, cwd: Path | None = None,
    expected_codes: tuple[int, ...] = (0,),
) -> None:
    try:
        result = subprocess.run(command, env=env, cwd=cwd, check=False)
    except OSError as error:
        raise ProvisionError(f"{label} could not start") from error
    if result.returncode not in expected_codes:
        expected = ", ".join(str(code) for code in expected_codes)
        raise ProvisionError(f"{label} exited {result.returncode}; expected {expected}")


def _devops_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Retain AWS/OIDC runtime access while excluding unrelated application credentials."""
    environment = dict(os.environ if source is None else source)
    for name in _DEVOPS_FORBIDDEN_ENV:
        environment.pop(name, None)
    return environment


def _resolve_aws_secret(reference: str, label: str = "scoped admin secret") -> str:
    try:
        result = subprocess.run(
            [
                "aws", "secretsmanager", "get-secret-value", "--secret-id", reference,
                "--query", "SecretString", "--output", "text",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProvisionError(f"AWS ECS AI delivery arc could not resolve its {label}") from error
    value = result.stdout.strip()
    if not value:
        raise ProvisionError(f"AWS ECS AI delivery arc resolved an empty {label}")
    return value


def _public_https_origin(value: str, label: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ProvisionError(f"{label} must be a credential-free public HTTPS origin")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        raise ProvisionError(f"{label} must not use a local or internal hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ProvisionError(f"{label} must not use a non-public IP address")
    return value.rstrip("/")


def _verify_console_candidate(origin: str, expected_sha: str, fetch=None) -> dict:
    """Prove the configured Console origin serves the manifest-pinned artifact."""
    base_url = _public_https_origin(origin, "AWS ECS Console origin")
    version_url = f"{base_url}/version.json"
    response = (fetch or make_fetch(timeout=10.0))(version_url)
    if response.status != 200:
        raise ProvisionError(
            f"AWS ECS Console candidate {version_url} returned HTTP {response.status}"
        )
    try:
        metadata = json.loads(response.body)
    except json.JSONDecodeError as error:
        raise ProvisionError("AWS ECS Console candidate returned invalid version.json") from error
    if not isinstance(metadata, dict) or metadata.get("name") != "honua-console":
        raise ProvisionError("AWS ECS Console candidate returned the wrong artifact identity")
    if metadata.get("commit") != expected_sha or metadata.get("shortCommit") != expected_sha[:12]:
        raise ProvisionError("AWS ECS Console candidate is not at the manifest-pinned commit")
    if metadata.get("areas") != ["studio", "catalog", "operate", "share"]:
        raise ProvisionError("AWS ECS Console candidate does not expose the complete area contract")
    version = metadata.get("version")
    if not isinstance(version, str) or not version or version == "unknown":
        raise ProvisionError("AWS ECS Console candidate has no releaseable version identity")
    return {
        "schemaVersion": "honua.release.console-candidate-evidence/v1",
        "origin": base_url,
        "versionUrl": version_url,
        "sourceSha": expected_sha,
        "version": version,
        "metadataSha256": hashlib.sha256(response.body.encode("utf-8")).hexdigest(),
    }


def _studio_command(phase: str) -> list[str]:
    if phase not in {"prepare", "resume"}:
        raise ValueError(f"unsupported Studio AI arc phase: {phase}")
    return ["npm", "run", "release:real-model-ai-arc", "--", phase, "--execute", "--yes"]


def _run_ai_arc_approval_boundary(
    *, studio_root: Path, console_root: Path, producer_env: dict[str, str],
    prepare_credential_ref: str, console_token_ref: str,
) -> None:
    # Model/admin credentials and the focused Console approval credential have deliberately
    # disjoint process environments. In particular, the Console producer refuses broad keys.
    boundary_env = dict(producer_env)
    for name in _AI_ARC_CHILD_CREDENTIAL_ENV:
        boundary_env.pop(name, None)
    studio_credential = _resolve_aws_secret(
        prepare_credential_ref, "scoped AI arc prepare credential"
    )
    try:
        studio_prepare_env = {
            **boundary_env,
            # The manifest-pinned Studio producer deliberately accepts only the
            # ordinary scoped HTTPS/MCP credential contract. Keep it out of the
            # inherited and Console environments, but pass the name it consumes.
            "HONUA_ADMIN_KEY": studio_credential,
        }
        _run_secretless(
            _studio_command("prepare"),
            env=studio_prepare_env,
            label="full Admin/GP/Studio real-model prepare",
            cwd=studio_root,
            expected_codes=(2,),
        )
        studio_prepare_env.pop("HONUA_ADMIN_KEY", None)

        console_token = _resolve_aws_secret(console_token_ref, "scoped Console approval token")
        try:
            _run_secretless(
                ["npm", "--prefix", "e2e/playwright", "run", "receipt:console"],
                env={**boundary_env, "HONUA_AI_ARC_CONSOLE_TOKEN": console_token},
                label="focused Console approval/audit/recovery producer",
                cwd=console_root,
            )
        finally:
            console_token = ""

        _run_secretless(
            _studio_command("resume"),
            # Studio validates the same scoped credential contract before
            # branching into resume. Console never inherits this value.
            env={**boundary_env, "HONUA_ADMIN_KEY": studio_credential},
            label="full Admin/GP/Studio real-model resume",
            cwd=studio_root,
        )
    finally:
        studio_credential = ""


def _ai_arc_paths() -> dict[str, Path]:
    out = Path(os.environ["HONUA_AI_ARC_OUT"])
    return {
        "out": out,
        "handoff": out / "handoff.json",
        "provisionEvidence": out / "provision-evidence.json",
        "provisionBinding": out / "provision-binding.json",
        "checkpoint": out / "checkpoint.json",
        "sdkReceipt": out / "sdk-journey.json",
        "consoleReceipt": out / "console-receipt.json",
        "sdkConsoleReceipt": out / "sdk-console-receipt.json",
        "consoleEvidence": out / "console-evidence.json",
        "consoleCandidate": out / "console-candidate-evidence.json",
        "modelHandoff": out / "real-model-handoff.json",
        "modelReceipt": out / "real-model-receipt.json",
        "modelEvidence": out / "real-model-evidence.json",
        "preTeardown": out / "pre-teardown-evidence.json",
        "teardownProof": out / "teardown-proof.json",
        "teardownEvidence": out / "teardown-evidence.json",
        "finalEvidence": out / "final-evidence.json",
        "provisionReceipt": out / "aws-ecs-provision-receipt.json",
        "arcReceipt": out / "aws-ecs-ai-delivery-arc-receipt.json",
    }


def _devops_resume_command(
    producer: Path, common: list[str], paths: dict[str, Path]
) -> list[str]:
    return [
        sys.executable, str(producer), "resume", *common,
        "--console-receipt", str(paths["consoleReceipt"]),
        "--sdk-console-receipt", str(paths["sdkConsoleReceipt"]),
        "--console-evidence", str(paths["consoleEvidence"]),
        "--real-model-handoff", str(paths["modelHandoff"]),
        "--real-model-receipt", str(paths["modelReceipt"]),
        "--real-model-evidence", str(paths["modelEvidence"]),
        "--pre-teardown-evidence", str(paths["preTeardown"]),
    ]


def _prepare_aws_ecs_ai_arc(target, endpoint: str, readiness: dict) -> dict:
    if not endpoint.startswith("https://"):
        raise ProvisionError(
            "AWS ECS AI delivery arc requires a public HTTPS endpoint; the current ECS target exposes HTTP"
        )
    missing = [name for name in _AI_ARC_REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise ProvisionError(f"AWS ECS AI delivery arc inputs are missing: {', '.join(missing)}")
    run_url = os.environ["HONUA_RUN_URL"]
    fixture_url = os.environ["HONUA_AI_ARC_FIXTURE_BASE_URL"]
    if not run_url.startswith("https://") or not fixture_url.startswith("https://"):
        raise ProvisionError("AWS ECS AI delivery arc requires HTTPS run evidence and fixture URLs")

    manifest_path = Path(
        os.environ.get("HONUA_PLATFORM_MANIFEST", str(Path(__file__).resolve().parents[1] / "platform-manifest.yaml"))
    )
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ProvisionError("AWS ECS AI delivery arc platform manifest is unavailable") from error
    components = manifest.get("components") if isinstance(manifest, dict) else None
    if not isinstance(components, dict):
        raise ProvisionError("AWS ECS AI delivery arc platform manifest has no components")
    devops_root = Path(os.environ["HONUA_DEVOPS_DIR"])
    sdk_root = Path(os.environ["HONUA_SDK_JS_DIR"])
    console_root = Path(os.environ["HONUA_CONSOLE_DIR"])
    studio_root = Path(os.environ["HONUA_STUDIO_DIR"])
    for name, root in (
        ("honua-devops", devops_root),
        ("honua-sdk-js", sdk_root),
        ("honua-console", console_root),
        ("honua-studio", studio_root),
    ):
        pinned = (components.get(name) or {}).get("sha")
        if _git_head(root) != pinned:
            raise ProvisionError(f"AWS ECS AI delivery arc {name} checkout is not manifest-pinned")
    console_candidate = _verify_console_candidate(
        os.environ["HONUA_AI_ARC_CONSOLE_ORIGIN"],
        components["honua-console"]["sha"],
    )
    producer = devops_root / "scripts" / "aws_ecs_ai_delivery_arc.py"
    if not producer.is_file():
        raise ProvisionError("manifest-pinned honua-devops has no AWS ECS AI delivery arc producer")

    provision = target.provision_evidence
    if (
        provision.get("terraformApply") != "passed"
        or len(provision.get("terraformPlanSha256", "")) != 64
    ):
        raise ProvisionError("AWS ECS AI delivery arc has no saved Terraform plan/apply evidence")
    try:
        db_host = target.output_json("db_endpoint")
        deploy_contract = target.output_json("deploy_contract")
        secret_refs = deploy_contract["secret_refs"]
        admin_ref = secret_refs["admin_password"]
        db_ref = secret_refs["db_connection"]
        prepare_credential_ref = os.environ["HONUA_AI_ARC_PREPARE_CREDENTIAL_SECRET_REF"]
    except (KeyError, TypeError, ProvisionError) as error:
        raise ProvisionError(
            "AWS ECS AI delivery arc Terraform outputs omit DB/admin secret-reference handoff"
        ) from error
    if not isinstance(db_host, str) or not db_host:
        raise ProvisionError("AWS ECS AI delivery arc Terraform handoff values are empty")
    if not all(isinstance(reference, str) and _AWS_SECRET_ARN.fullmatch(reference)
               for reference in (admin_ref, db_ref, prepare_credential_ref)):
        raise ProvisionError("AWS ECS AI delivery arc handoff secret references are not AWS ARNs")
    if prepare_credential_ref == admin_ref:
        raise ProvisionError(
            "AWS ECS AI delivery arc prepare credential must not reuse the bootstrap admin secret"
        )

    server = components.get("honua-server") or {}
    expected_image = f"{server.get('image')}@{server.get('digest')}"
    if os.environ.get("HONUA_ECS_IMAGE") != expected_image:
        raise ProvisionError("AWS ECS target did not install the exact manifest image@digest")
    candidate_id = f"manifest-sha256:{_sha256(manifest_path)}"
    shas = {
        name: (components.get(name) or {}).get("sha")
        for name in ("honua-server", "honua-devops", "honua-iac")
    }
    paths = _ai_arc_paths()
    _write_json(paths["consoleCandidate"], console_candidate)
    handoff = {
        "schemaVersion": "honua.mcp-proxy.handoff/v1",
        "env": {"HONUA_BASE_URL": endpoint, "HONUA_MCP_REMOTE_URL": f"{endpoint.rstrip('/')}/mcp"},
        "secretRefs": {"HONUA_ADMIN_KEY": admin_ref},
    }
    _write_json(paths["handoff"], handoff)
    provision_evidence = {
        "schemaVersion": "honua.release.aws-ecs-provision-evidence/v1",
        "candidateId": candidate_id,
        "consoleCandidate": console_candidate,
        "releaseId": manifest.get("platformRelease"),
        "endpoint": endpoint,
        "serverImage": expected_image,
        "components": shas,
        "terraformPlanSha256": provision["terraformPlanSha256"],
        "terraformApply": "passed",
        "readiness": readiness,
        "handoffSha256": _sha256(paths["handoff"]),
    }
    _write_json(paths["provisionEvidence"], provision_evidence)
    binding = {
        "schemaVersion": "honua.aws-ecs.provision-binding/v1",
        "target": "aws-ecs",
        "status": "ready",
        "candidateId": candidate_id,
        "releaseId": manifest.get("platformRelease"),
        "endpoint": endpoint.rstrip("/"),
        "adminKeySecretRef": admin_ref,
        "serverImage": expected_image,
        "components": shas,
        "checks": {
            "terraform-plan": "passed",
            "terraform-apply": "passed",
            "readiness": "passed",
            "admin-mcp-handoff": "passed",
        },
        "evidence": {"url": run_url, "sha256": _sha256(paths["provisionEvidence"])},
    }
    _write_json(paths["provisionBinding"], binding)

    common = [
        "--manifest", str(manifest_path), "--sdk-root", str(sdk_root),
        "--handoff", str(paths["handoff"]), "--provision-binding", str(paths["provisionBinding"]),
        "--fixture-base-url", fixture_url, "--db-host", db_host,
        "--db-connection-secret-ref", db_ref, "--checkpoint", str(paths["checkpoint"]),
        "--sdk-receipt", str(paths["sdkReceipt"]), "--source-sha", components["honua-devops"]["sha"],
    ]
    _run_secretless(
        [sys.executable, str(producer), "prepare", *common],
        env=_devops_environment(),
        label="AWS ECS AI arc prepare",
    )

    producer_env = {
        **os.environ,
        "HONUA_AI_ARC_CHECKPOINT": str(paths["checkpoint"]),
        "HONUA_AI_ARC_CONSOLE_RECEIPT": str(paths["consoleReceipt"]),
        "HONUA_AI_ARC_SDK_CONSOLE_RECEIPT": str(paths["sdkConsoleReceipt"]),
        "HONUA_AI_ARC_CONSOLE_EVIDENCE": str(paths["consoleEvidence"]),
        "HONUA_AI_ARC_CONSOLE_RECEIPT_SCHEMA": str(
            sdk_root / "mcp" / "release" / "zero-to-map" / "contracts" / "console-receipt.schema.json"
        ),
        "HONUA_AI_ARC_CONSOLE_ORIGIN": os.environ["HONUA_AI_ARC_CONSOLE_ORIGIN"].rstrip("/"),
        "HONUA_AI_ARC_REAL_MODEL_HANDOFF": str(paths["modelHandoff"]),
        "HONUA_AI_ARC_REAL_MODEL_RECEIPT": str(paths["modelReceipt"]),
        "HONUA_AI_ARC_REAL_MODEL_EVIDENCE": str(paths["modelEvidence"]),
        "HONUA_AI_ARC_PROVISION_BINDING": str(paths["provisionBinding"]),
        "HONUA_AI_ARC_ENDPOINT": endpoint.rstrip("/"),
        "HONUA_AI_ARC_SDK_PLAN": str(
            sdk_root / "mcp" / "release" / "zero-to-map" / "journey.v1.json"
        ),
        "HONUA_PLATFORM_MANIFEST": str(manifest_path),
        "HONUA_AI_ARC_EVIDENCE_URL": run_url,
    }
    _run_ai_arc_approval_boundary(
        studio_root=studio_root,
        console_root=console_root,
        producer_env=producer_env,
        prepare_credential_ref=prepare_credential_ref,
        console_token_ref=os.environ["HONUA_AI_ARC_CONSOLE_TOKEN_SECRET_REF"],
    )
    _run_secretless(
        _devops_resume_command(producer, common, paths),
        env=_devops_environment(),
        label="AWS ECS AI arc resume",
    )
    return {
        "paths": paths,
        "manifest": manifest,
        "manifestPath": manifest_path,
        "producer": producer,
        "runUrl": run_url,
        "candidateId": candidate_id,
    }


def _finalize_aws_ecs_ai_arc(target, context: dict) -> None:
    teardown = target.teardown_evidence
    if teardown != {"terraformDestroy": "passed", "cleanupVerified": "passed"}:
        raise ProvisionError("AWS ECS AI delivery arc has no verified Terraform teardown evidence")
    paths = context["paths"]
    manifest = context["manifest"]
    components = manifest["components"]
    proof = {
        "schemaVersion": "honua.release.aws-ecs-teardown-proof/v1",
        "candidateId": context["candidateId"],
        "releaseId": manifest["platformRelease"],
        "checks": teardown,
    }
    _write_json(paths["teardownProof"], proof)
    teardown_evidence = {
        "schemaVersion": "honua.aws-ecs.teardown-evidence/v1",
        "target": "aws-ecs",
        "status": "passed",
        "candidateId": context["candidateId"],
        "releaseId": manifest["platformRelease"],
        "components": {
            name: components[name]["sha"] for name in ("honua-devops", "honua-iac")
        },
        "checks": {"terraform-destroy": "passed", "cleanup-verified": "passed"},
        "evidence": {"url": context["runUrl"], "sha256": _sha256(paths["teardownProof"])},
    }
    _write_json(paths["teardownEvidence"], teardown_evidence)
    _run_secretless(
        [
            sys.executable, str(context["producer"]), "finalize",
            "--manifest", str(context["manifestPath"]),
            "--pre-teardown-evidence", str(paths["preTeardown"]),
            "--teardown-evidence", str(paths["teardownEvidence"]),
            "--evidence-url", context["runUrl"],
            "--final-evidence", str(paths["finalEvidence"]),
            "--provision-receipt", str(paths["provisionReceipt"]),
            "--arc-receipt", str(paths["arcReceipt"]),
        ],
        env=_devops_environment(),
        label="AWS ECS AI arc finalization",
    )


def _wait_for_endpoint(endpoint: str, fetch, *, attempts: int = _READY_ATTEMPTS,
                       delay_seconds: float = _READY_DELAY_SECONDS,
                       sleep=time.sleep) -> tuple[bool, dict]:
    """Wait for the deployed route, Lambda cold start, and application readiness.

    Terraform can finish while an API Gateway auto-deployment is still propagating. A newly
    published Lambda alias also needs one cold start before the canonical probes are meaningful.
    Treat every non-200 response as not-ready and preserve the final response as gate evidence;
    the canonical checks still run after timeout so they retain their detailed verdicts.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    url = endpoint.rstrip("/") + "/healthz/ready"
    last = None
    for attempt in range(1, attempts + 1):
        last = fetch(url)
        if last.status == 200:
            return True, {"url": url, "status": 200, "attempts": attempt}
        if attempt < attempts:
            sleep(delay_seconds)
    assert last is not None
    return False, {
        "url": url,
        "status": last.status,
        "attempts": attempts,
        "body_head": last.body[:200],
        "headers": last.headers,
    }


def run(target_name: str, require_real: bool, reference_endpoint: str | None,
        redis_enabled: bool = False) -> dict:
    cls = REGISTRY.get(target_name)
    if cls is None:
        return {"gate": "cloud-parity", "target": target_name, "status": "fail",
                "why": f"unknown target {target_name!r}; known: {sorted(REGISTRY)}"}

    target = cls(run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    redis_mode = "redis-on" if redis_enabled else "redis-off"
    cell = f"{target_name}/{redis_mode}"
    avail = target.availability()

    report: dict = {"gate": "cloud-parity", "target": target_name, "redis": redis_mode, "cell": cell,
                    "require_real": require_real,
                    "availability": {"ok": avail.ok, "reason": avail.reason, "missing": avail.missing}}

    if not avail.ok:
        # Cloud/OIDC creds unset => SELF-SKIP (not blocked, not fail), even under require_real: without
        # creds this tier literally cannot run, and a local cut must not be reddened by it. Enforcement
        # is per-RC: an org wires HONUA_AWS_ROLE_ARN for a candidate and the cell then runs for real.
        if not _cloud_creds_present():
            report["status"] = "skipped"
            report["why"] = "cloud-creds-unset"
            return report
        # Creds present but infra half-wired (no image / no IaC tree) => BLOCKED, promoted to FAIL under
        # require_real so a genuinely broken cloud path is a real red.
        report["status"] = "fail" if require_real else "blocked"
        report["why"] = avail.reason
        return report

    endpoint = None
    checks = []
    canary_results = []
    extended = []
    ai_arc_context = None
    teardown_completed = False
    try:
        _mark_provision_attempt()
        endpoint = target.provision(redis_enabled=redis_enabled)
        report["endpoint"] = endpoint
        fetch = make_fetch(timeout=10.0)
        # The budget is read from the module globals at CALL time so a test can shorten it; the
        # defaults on _wait_for_endpoint are bound at def time and cannot be monkeypatched.
        ready, readiness = _wait_for_endpoint(endpoint, fetch, attempts=_READY_ATTEMPTS,
                                              delay_seconds=_READY_DELAY_SECONDS)
        report["readiness"] = {"ready": ready, **readiness}
        checks = run_canonical(endpoint, fetch)
        report["checks"] = _check_dicts(checks)
        # Cloud-tier unblock (honua-release#61): the canary probe set, GENERIC mode (no service/tile id
        # configured — nothing is seeded on a bare terraform cell yet), so data-dependent probes report
        # BLOCKED honestly rather than a fake pass/fail; reachability-only probes run for real.
        canary_results = canary_probes.run_canary(endpoint, fetch)
        report["canaryProbes"] = _check_dicts(canary_results)
        # The complete delivery arc is the AWS ECS certification target from
        # honua-release#129. Serverless and EKS run the slim target-parity set;
        # treating their intentionally absent ECS journey as a required-real
        # failure would make the full release matrix impossible to certify.
        # Keep the ECS journey inside the provision/teardown try/finally so the
        # driver never runs against an environment that has already been torn down.
        extended = run_extended(endpoint) if target_name == "aws-ecs" else []
        report["scenarioCoverage"] = _check_dicts(extended)
        if target_name == "aws-ecs" and require_real:
            ai_arc_context = _prepare_aws_ecs_ai_arc(target, endpoint, report["readiness"])
            arc_evidence = ai_arc_context["paths"]["preTeardown"]
            extended = [
                CheckResult(
                    name,
                    "pass",
                    "candidate-bound AWS ECS AI delivery arc passed before teardown",
                    {"path": str(arc_evidence), "sha256": _sha256(arc_evidence)},
                )
                for name, _description in EXTENDED_SCENARIOS
            ]
            report["scenarioCoverage"] = _check_dicts(extended)
            report["aiDeliveryArc"] = {
                "status": "passed-before-teardown",
                "checkpoint": str(ai_arc_context["paths"]["checkpoint"]),
                "preTeardownEvidence": str(ai_arc_context["paths"]["preTeardown"]),
                "consoleCandidateEvidence": str(ai_arc_context["paths"]["consoleCandidate"]),
            }
    except ProvisionError as e:
        report["status"] = "fail"
        report["why"] = f"provision failed: {e}"
    finally:
        # Teardown ALWAYS runs, including on the failure path: a cell that created real AWS
        # infrastructure and then failed must not strand it (honua-iac#142 — orphaned VPCs/clusters
        # bill until someone reaps them by hand). A teardown that cannot complete is itself a hard
        # failure of the cell, because the orphan is real — it is never swallowed.
        try:
            target.teardown(redis_enabled=redis_enabled)
            teardown_completed = True
        except ProvisionError as e:
            prior = report.get("why")
            report["status"] = "fail"
            report["why"] = f"{prior}; teardown failed: {e}" if prior else f"teardown failed: {e}"
        if ai_arc_context is not None and teardown_completed:
            try:
                _finalize_aws_ecs_ai_arc(target, ai_arc_context)
                report["aiDeliveryArc"] = {
                    "status": "passed",
                    "provisionReceipt": str(ai_arc_context["paths"]["provisionReceipt"]),
                    "journeyReceipt": str(ai_arc_context["paths"]["arcReceipt"]),
                    "modelReceipt": str(ai_arc_context["paths"]["modelReceipt"]),
                    "modelEvidence": str(ai_arc_context["paths"]["modelEvidence"]),
                    "modelHandoff": str(ai_arc_context["paths"]["modelHandoff"]),
                    "consoleEvidence": str(ai_arc_context["paths"]["consoleEvidence"]),
                    "consoleCandidateEvidence": str(ai_arc_context["paths"]["consoleCandidate"]),
                    "finalEvidence": str(ai_arc_context["paths"]["finalEvidence"]),
                }
            except ProvisionError as e:
                prior = report.get("why")
                report["status"] = "fail"
                report["why"] = (
                    f"{prior}; AI delivery arc finalization failed: {e}"
                    if prior else f"AI delivery arc finalization failed: {e}"
                )

    if report.get("status") == "fail":
        return report

    # Verdict from the canonical set + the canary probes' genuine failures.
    failed = [c.name for c in checks if c.status == "fail"]
    canary_failed = [c.name for c in canary_results if c.status == "fail"]
    blocked = [c.name for c in checks if c.status == "blocked"]
    ext_blocked = [c.name for c in extended if c.status in ("blocked", "fail")]

    # honua-release#128: a cell whose terraform applied but whose endpoint never served is a FAILED
    # cell, and it is reported as that one fact rather than as a wall of derived probe failures. The
    # readiness poll above already spent its full budget on /healthz/ready; if it never got a 200 and
    # the probes then could not reach the endpoint either, the deployment did not come up. Naming it
    # here keeps the diagnosis at the top of the report instead of leaving the reader to infer it from
    # twenty identical timeouts.
    unreached = [c.name for c in list(checks) + list(canary_results) if is_endpoint_unreachable(c)]
    never_ready = not report.get("readiness", {}).get("ready", True)
    if unreached or never_ready:
        reasons = []
        if never_ready:
            reasons.append("the readiness poll never got a 200 from /healthz/ready within its full "
                           f"budget ({report['readiness'].get('attempts')} attempts, last status "
                           f"{report['readiness'].get('status')})")
        if unreached:
            reasons.append(f"these checks could not reach it at all: {unreached}")
        report["status"] = "fail"
        report["why"] = (
            f"{cell}: terraform provisioned {endpoint} but it never served — " + "; ".join(reasons)
            + ". The endpoint is the thing under test, so this is a cell failure, not a skip "
              "(honua-release#128)."
        )
        return report

    if failed or canary_failed:
        report["status"] = "fail"
        report["why"] = f"canonical checks failed on {cell}: {failed}; canary probes failed: {canary_failed}"
        return report
    if require_real and (blocked or ext_blocked):
        report["status"] = "fail"
        report["why"] = (f"require_real on {cell}: canonical blocked={blocked or '[]'}, "
                         f"scenarios not-certified={ext_blocked} (needs honua-release#129 harness image)")
        return report

    # Parity vs the reference target, when one was provided.
    if reference_endpoint:
        ref_checks = run_canonical(reference_endpoint)
        report["reference_checks"] = _check_dicts(ref_checks)
        verdict = compare(
            TargetRun("local-docker", provisioned=True, results=ref_checks),
            TargetRun(cell, provisioned=True, results=checks),
        )
        report["parity"] = {"status": verdict.status, "why": verdict.why, "diffs": verdict.diffs}
        if verdict.status == "fail":
            report["status"] = "fail"
            report["why"] = f"parity divergence: {verdict.why}"
            return report

    report["status"] = "blocked" if (blocked and not require_real) else "pass"
    # Say what actually happened. The old wording claimed "canonical set passed" even for a cell whose
    # canonical set was entirely BLOCKED — the sentence that made honua-release#128 invisible in the
    # job log for as long as it existed.
    report["why"] = report.get("why") or (
        f"{cell}: canonical set " + (f"blocked on {blocked}" if blocked else "passed")
        + (" + parity ok" if reference_endpoint else " (parity skipped: no reference endpoint)"))
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="aws-serverless", choices=sorted(REGISTRY))
    ap.add_argument("--redis", choices=["on", "off"], default="off",
                    help="run the target with Redis enabled or disabled (parity must hold either way)")
    ap.add_argument("--require-real", action="store_true",
                    help="promote BLOCKED to FAIL (the train / a real nightly run)")
    ap.add_argument("--reference-endpoint", default=os.environ.get("HONUA_REFERENCE_ENDPOINT") or None,
                    help="a reference (local-docker) endpoint to assert parity against")
    args = ap.parse_args(argv)

    report = run(args.target, args.require_real, args.reference_endpoint, redis_enabled=(args.redis == "on"))
    report.setdefault("evidence_url", os.environ.get("HONUA_RUN_URL", ""))
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"== cloud-parity :: {report['cell']} -> {report['status'].upper()} ==")
    print(f"   {report.get('why', '')}")
    if report["status"] == "skipped":
        # A clear, machine-greppable notice so the self-skip is obvious in the job log / summary.
        print(f"::notice title=cloud-cert self-skipped::{report['cell']}: cloud-creds-unset "
              "(set HONUA_AWS_ROLE_ARN to enforce this tier per-RC)")
    for c in report.get("checks", []):
        print(f"   [{c['status'].upper():7}] {c['name']}: {c['why']}")
    for c in report.get("canaryProbes", []):
        print(f"   canary [{c['status'].upper():7}] {c['name']}: {c['why']}")
    for c in report.get("scenarioCoverage", []):
        print(f"   scenario [{c['status'].upper():7}] {c['name']}: {c['why']}")
    if "parity" in report:
        print(f"   parity: {report['parity']['status']} — {report['parity']['why']}")
    print(f"   (written to {REPORT_PATH})")

    # `run()` already escalates BLOCKED -> "fail" under require_real, so a residual "blocked" here means
    # it is being tolerated (bootstrap, no infra yet) — exit 0, surfaced in the report, not a fake green.
    # Only a real "fail" reddens the job. Mirrors the local-docker tier's honest-bootstrap behaviour.
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
