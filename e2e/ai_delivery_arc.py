#!/usr/bin/env python3
"""Consume the manifest-pinned SDK's D9.3 journey and emit a release receipt.

Contract mode is the default. A live run is explicit and requires all external
inputs; missing component pins or receipts are reported BLOCKED and become a
hard failure when E2E_REQUIRE_REAL=1.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path(os.environ.get("E2E_OUT", str(REPO_ROOT / "e2e" / "out")))
SDK_ROOT = Path(os.environ.get("E2E_SDK_JS_DIR", str(REPO_ROOT / "_sdk-js")))
SDK_RECEIPT = OUT_DIR / "sdk-zero-to-map-receipt.json"
RELEASE_RECEIPT = OUT_DIR / "ai-delivery-arc-receipt.json"
EXTERNAL_RECEIPT_ENV = (
    ("aws-ecs-provision", "E2E_AI_AWS_RECEIPT"),
    ("aws-ecs-ai-delivery-arc", "E2E_AI_AWS_ARC_RECEIPT"),
    ("studio-real-model", "E2E_AI_STUDIO_MODEL_RECEIPT"),
)


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def checker(*, mode: str, include_receipt: bool, require_real: bool) -> int:
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "check_ai_delivery_arc.py"),
        "--manifest",
        str(REPO_ROOT / "platform-manifest.yaml"),
        "--contract",
        str(REPO_ROOT / "certification" / "ai-delivery-arc.yaml"),
        "--sdk-root",
        str(SDK_ROOT),
        "--mode",
        mode,
        "--json-out",
        str(RELEASE_RECEIPT),
    ]
    if include_receipt:
        command.extend(("--sdk-receipt", str(SDK_RECEIPT)))
        for receipt_id, env_name in EXTERNAL_RECEIPT_ENV:
            if os.environ.get(env_name):
                command.extend(("--external-receipt", f"{receipt_id}={os.environ[env_name]}"))
    if require_real:
        command.append("--require-real")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def write_blocked(message: str) -> None:
    if RELEASE_RECEIPT.is_file():
        report = json.loads(RELEASE_RECEIPT.read_text(encoding="utf-8"))
        report["status"] = "blocked" if not report.get("errors") else "fail"
        report.setdefault("errors", [])
        report.setdefault("blockers", []).append(message)
    else:
        report = {
            "schemaVersion": "honua.ai-delivery-arc-release-receipt/v1",
            "releaseContract": "honua-release#123/D9.3",
            "status": "blocked",
            "errors": [],
            "blockers": [message],
        }
    RELEASE_RECEIPT.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    require_real = truthy("E2E_REQUIRE_REAL")
    live = truthy("E2E_RUN_AI_DELIVERY_ARC") or require_real
    mode = "live" if live else "contract"

    # Validate the source/pins/plan before executing code from the component checkout.
    initial = checker(mode=mode, include_receipt=False, require_real=False)
    if initial != 0:
        return initial
    if not SDK_ROOT.is_dir():
        return 1 if require_real else 0
    plan = SDK_ROOT / "mcp" / "release" / "zero-to-map" / "journey.v1.json"
    package = SDK_ROOT / "mcp" / "package.json"
    if not plan.is_file() or not package.is_file():
        # The checker already wrote the precise component blocker.
        return 1 if require_real else 0

    if live:
        required = {
            "E2E_AI_FIXTURE_BASE_URL": os.environ.get("E2E_AI_FIXTURE_BASE_URL"),
            "E2E_AI_DB_PASSWORD": os.environ.get("E2E_AI_DB_PASSWORD"),
            "E2E_AI_CONSOLE_RECEIPT": os.environ.get("E2E_AI_CONSOLE_RECEIPT"),
            **{env_name: os.environ.get(env_name) for _, env_name in EXTERNAL_RECEIPT_ENV},
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            write_blocked(f"live journey inputs are missing: {', '.join(missing)}")
            print(f"AI delivery arc: BLOCKED (missing {', '.join(missing)})")
            return 1 if require_real else 0

    command = [
        "npm",
        "run",
        "release:zero-to-map",
        "--",
        "--output",
        str(SDK_RECEIPT),
    ]
    if live:
        cli = os.environ.get("E2E_HONUA_CLI_COMMAND", str(SDK_ROOT / "dist" / "src" / "cli" / "bin.js"))
        command.extend(
            (
                "--execute",
                "--yes",
                "--mcp-url",
                os.environ.get("HONUA_MCP_REMOTE_URL", "http://localhost:8080/mcp"),
                "--honua-command",
                cli,
                "--var",
                f"fixtureBaseUrl={os.environ['E2E_AI_FIXTURE_BASE_URL']}",
                "--var",
                f"dbPassword={os.environ['E2E_AI_DB_PASSWORD']}",
                "--console-receipt",
                os.environ["E2E_AI_CONSOLE_RECEIPT"],
            )
        )

    run = subprocess.run(command, cwd=SDK_ROOT / "mcp", check=False)
    # Contract mode intentionally exits 2; the receipt is the verdict. Live mode
    # may exit 1/2, and the checker converts its first broken action into attribution.
    if not SDK_RECEIPT.is_file():
        write_blocked(f"SDK journey exited {run.returncode} without writing {SDK_RECEIPT}")
        return 1 if require_real else 0

    result = checker(mode=mode, include_receipt=True, require_real=require_real)
    report = json.loads(RELEASE_RECEIPT.read_text(encoding="utf-8"))
    print(
        f"AI delivery arc {mode}: {report.get('status', 'unknown').upper()} "
        f"(SDK exit {run.returncode}; receipt {RELEASE_RECEIPT})"
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
