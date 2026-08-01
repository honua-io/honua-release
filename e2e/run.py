#!/usr/bin/env python3
"""Phase A local-docker E2E seam-harness entrypoint.

Brings up the real honua-server + DB (docker-compose), installs the SDKs from staging, runs the
canonical scenarios with NO mocks at the seam, and emits a machine-readable gate-report.json that the
release train's gate_e2e consumes.

Honesty rules (AGENTS.md — a gate that can't fail is worse than no gate):
  - the docker-compose static check + the Python import/compile of every scenario ALWAYS run; a broken
    edit makes the gate red even with no images.
  - server-dependent scenarios are BLOCKED (not PASS) while the manifest pins are placeholders.
  - E2E_REQUIRE_REAL=1 promotes BLOCKED/SKIPPED to FAIL (used once images+metrics are real, and in the
    release train) so the gate can genuinely FAIL.

Exit code: 0 only if the assembled report status is "pass"; non-zero otherwise.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))  # make `import runner.*` work for the scenarios

from runner import harness  # noqa: E402
from runner.manifest import load_manifest  # noqa: E402
from runner.report import Result, Status, assemble, write_report  # noqa: E402

# Registry of canonical scenarios (decomposed SDK × scenario matrix runs here on local docker).
SCENARIO_FILES = [
    E2E_DIR / "scenarios" / "geoservices_error_surfacing" / "scenario.py",
    E2E_DIR / "scenarios" / "sync_no_duplicates" / "scenario.py",
]

REPORT_PATH = E2E_DIR / "gate-report.json"


def _load_scenario(path: Path):
    spec = importlib.util.spec_from_file_location(f"scenario_{path.parent.name}", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # import-compiles the scenario — a broken scenario fails the gate
    return mod


def main() -> int:
    require_real = os.environ.get("E2E_REQUIRE_REAL", "") not in ("", "0", "false", "False")
    manifest = load_manifest()
    server_port = os.environ.get("HONUA_SERVER_PORT", "8080")
    server_url = f"http://localhost:{server_port}"
    metrics_url = f"{server_url}/metrics"

    print(f"== Honua e2e local-docker :: platform {manifest.platform_release} ==")
    print(f"   server image pin: {manifest.server_image} (real={manifest.server.is_real})")
    print(f"   require_real={require_real}")

    results: list[Result] = []

    # --- always-runnable gate: compose file is valid -------------------------------------------
    cfg_ok, cfg_why = harness.compose_config_ok()
    if not cfg_ok and "docker CLI not found" not in cfg_why:
        # A genuinely broken compose file is a hard FAIL regardless of images.
        results.append(Result("compose-config", Status.FAIL, why=cfg_why))
    else:
        results.append(Result(
            "compose-config",
            Status.PASS if cfg_ok else Status.SKIPPED,
            why=cfg_why,
        ))

    # --- load scenarios (import errors surface as a hard FAIL) ----------------------------------
    scenarios = []
    for f in SCENARIO_FILES:
        try:
            scenarios.append(_load_scenario(f))
        except Exception as e:  # noqa: BLE001
            results.append(Result(f.parent.name, Status.FAIL, why=f"scenario failed to import: {e}"))

    # --- bring up the stack --------------------------------------------------------------------
    server_up = False
    block_reason = ""
    if cfg_ok:
        try:
            harness.compose_up(manifest, server_url)
            server_up = True
        except harness.ImageUnavailable as e:
            block_reason = str(e)
            print(f"   server not available (scenarios BLOCKED): {e}")
        except Exception as e:  # noqa: BLE001 - any bringup error blocks server scenarios
            block_reason = f"compose up failed: {e}"
            print(f"   {block_reason}")
    else:
        block_reason = cfg_why

    try:
        if server_up:
            sdk_notes = harness.install_sdks(manifest)
            print(f"   sdk install: {sdk_notes}")

        ctx = harness.Ctx(
            manifest=manifest,
            server_url=server_url,
            metrics_url=metrics_url,
            probes_python=sys.executable,
            require_real=require_real,
        )

        for mod in scenarios:
            meta = mod.META
            if meta.get("requires_server") and not server_up:
                results.append(Result(
                    meta["name"], Status.BLOCKED, seeded_from=meta.get("seeded_from", ""),
                    why=f"server not up: {block_reason}",
                ))
                continue
            try:
                results.append(mod.run(ctx))
            except Exception as e:  # noqa: BLE001 - a crashing scenario is a FAIL
                results.append(Result(
                    meta["name"], Status.FAIL, seeded_from=meta.get("seeded_from", ""),
                    why=f"scenario raised: {e}",
                ))
    finally:
        if cfg_ok:
            harness.compose_down()

    report = assemble(results, require_real)
    write_report(report, REPORT_PATH)

    print("\n== gate-report ==")
    for r in report["scenarios"]:
        print(f"  [{r['status'].upper():7}] {r['scenario']}: {r['why']}")
    print(f"  summary: {report['summary']}")
    print(f"  OVERALL: {report['status'].upper()}  (written to {REPORT_PATH})")

    return 0 if report["status"] == Status.PASS.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
