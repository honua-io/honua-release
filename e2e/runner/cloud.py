"""Run the existing seam drivers while a cloud cell is alive; retain local report rows."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from canonical_checks import CheckResult
from harness.cloud.seed import seed

E2E = Path(__file__).resolve().parents[1]
DRIVERS = {
    "mcp-handshake": ("mcp", ["S1-mcp-handshake", "S2-mcp-tool-catalog"]),
    "studio-authoring": ("studio", ["S3-studio-authoring"]),
    "gp-execute": ("gp", ["S5-geoprocessing"]),
    # The demo driver declares its inventory. Read that contract rather than duplicating names.
    "top-demo": ("demos", [f"S9-demos-{name}" for name in
        (E2E / "drivers/demos/run.sh").read_text().split("DEMOS=(", 1)[1].split(")", 1)[0].split()]),
}


def run_extended(endpoint, *, target, out, require_real=False):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Drivers carry only the application key. Cloud credentials and dispatch tokens never reach
    # npm, browser pages or driver subprocesses. No subprocess output containing a key is retained.
    env = {k: v for k, v in os.environ.items() if k in (
        "PATH", "HOME", "LANG", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH",
        "E2E_SITE_DIR", "E2E_SITE_SHA", "E2E_PW_HOME", "E2E_PLAYWRIGHT_VERSION")}
    env.update(E2E_BASE=endpoint.rstrip("/"), E2E_API_KEY=target.admin_api_key,
        E2E_OUT=str(out), E2E_REQUIRE_REAL="1" if require_real else "",
        E2E_SERVER_BOOTED="true", E2E_SITE_PORT="18099")
    seed_error = None
    try:
        seed(endpoint, target.admin_api_key, target, out)
    except Exception as error:
        # HTTP responses and database errors can contain credentials; preserve only the type.
        seed_error = type(error).__name__
    fragments = out / "scenarios.jsonl"
    fragments.write_text("")
    results = []
    for name, (driver, expected) in DRIVERS.items():
        before = len(fragments.read_text().splitlines())
        try:
            completed = subprocess.run(["bash", str(E2E / "drivers" / driver / "run.sh")],
                env=env, capture_output=True, text=True, timeout=900)
            code = completed.returncode
        except (OSError, subprocess.TimeoutExpired):
            code = -1
        try:
            rows = [json.loads(line) for line in fragments.read_text().splitlines()[before:]]
            if not all(isinstance(row, dict) and "why" in row and "status" in row for row in rows):
                raise ValueError("invalid scenario row")
        except (ValueError, TypeError):
            rows = []
            code = -1
        for scenario in expected:
            matches = [row for row in rows if row.get("scenario") == scenario]
            if len(matches) != 1:
                rows = [row for row in rows if row.get("scenario") != scenario]
                rows.append({"scenario": scenario, "status": "fail",
                    "why": "driver did not emit exactly one required verdict", "evidence": None})
        if code != 0:
            rows.append({"scenario": f"{name}-driver", "status": "fail",
                "why": f"driver exited {code}", "evidence": None})
        if seed_error and driver in ("studio", "demos"):
            rows.append({"scenario": f"{name}-seed", "status": "fail",
                "why": f"cloud fixture publication failed: {seed_error}", "evidence": None})
        # Write the normalized rows back, including missing-verdict failures, for the same assembler
        # used by the local harness. Redact the application key before persisting any driver evidence.
        retained = fragments.read_text().splitlines()[:before]
        retained += [json.dumps(row).replace(target.admin_api_key, "[redacted]") for row in rows]
        fragments.write_text("\n".join(retained) + "\n")
        rows = [json.loads(line) for line in retained[before:]]
        states = [row.get("status") for row in rows]
        status = "fail" if any(s not in ("pass", "blocked") for s in states) else (
            "blocked" if "blocked" in states else "pass")
        results.append(CheckResult(name, status, "; ".join(
            f"{r['scenario']}: {r['why']}" for r in rows), {"scenarios": rows}))
    assembler = f'source "{E2E}/harness/lib/report.sh"; assemble_report "$E2E_OUT"'
    subprocess.run(["bash", "-c", assembler], env=env, capture_output=True, text=True, check=False)
    # Detailed browser output is diagnostic input, not a publishable credential-safe artifact.
    for filename in ("demos-results.json", "demos-drive.log", "demos-site.log", "s3-gate-state.json"):
        (out / filename).unlink(missing_ok=True)
    return results
