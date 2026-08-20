"""Canonical scenario: GeoServices error surfacing.

Seeded from sdk-js#309, sdk-python#122, server#2243.

Assert: when the server returns an HTTP 200 with a GeoServices `{error: ...}` body, EVERY SDK must
RAISE (not return success), and the in-band error metric `honua_geoservices_error_total` must increment.
This couples the SDK contract bug (clients trusting the 200 status line) to the telemetry gate (a server
blind to its own error rate). NO mocks: each SDK probe hits the real composed server.

Mechanics:
  1. scrape honua_geoservices_error_total (before)
  2. run each language probe; each forces a 200+{error} via the SDK and exits 0 only if the SDK raised
  3. scrape the metric again (after) and assert it increased by at least the number of erroring calls
"""
from __future__ import annotations

import time
from pathlib import Path

from runner.harness import Ctx, run_probe, scrape_metric
from runner.report import Result, Status

META = {
    "name": "geoservices-error-surfacing",
    "seeded_from": "sdk-js#309, sdk-python#122, server#2243",
    "requires_server": True,
}

ERROR_METRIC = "honua_geoservices_error_total"
PROBES_DIR = Path(__file__).parent / "probes"
PROBES = {
    "python": PROBES_DIR / "probe.py",
    "js": PROBES_DIR / "probe.mjs",
    "dotnet": PROBES_DIR / "dotnet",  # project dir for `dotnet run`
}


def run(ctx: Ctx) -> Result:
    evidence: dict = {"probes": {}, "metric": ERROR_METRIC}

    before = scrape_metric(ctx.metrics_url, ERROR_METRIC)
    evidence["metric_before"] = before
    baseline = before if before is not None else 0.0

    erroring_calls = 0
    completed_probes = 0
    failures: list[str] = []

    for short, path in PROBES.items():
        if not ctx.sdk_available(short):
            evidence["probes"][short] = "skipped: toolchain unavailable"
            failures.append(f"{short}: required SDK toolchain unavailable")
            continue
        if not ctx.manifest.sdks[short].is_real:
            # SDK pin is a placeholder — the probe would have nothing real to import.
            evidence["probes"][short] = "blocked: SDK version is a placeholder (TBD)"
            failures.append(f"{short}: SDK pin is not a real frozen version")
            continue

        probe_before_raw = scrape_metric(ctx.metrics_url, ERROR_METRIC)
        probe_before = probe_before_raw if probe_before_raw is not None else 0.0
        pr = run_probe(short, path, ctx)
        probe_after = None
        for attempt in range(3):
            probe_after = scrape_metric(ctx.metrics_url, ERROR_METRIC)
            if probe_after is not None and probe_after - probe_before >= 1:
                break
            if attempt < 2:
                time.sleep(1)
        evidence["probes"][short] = {
            "exit": pr.exit_code,
            "stdout": pr.stdout.strip()[-500:],
            "stderr": pr.stderr.strip()[-500:],
            "metric_before": probe_before_raw,
            "metric_after": probe_after,
        }
        if pr.skipped:
            failures.append(f"{short}: frozen SDK was not installed/importable")
            continue
        completed_probes += 1
        erroring_calls += 1
        if pr.failed:
            # The audit bug: SDK returned success on a 200+{error}.
            failures.append(f"{short}: SDK did not raise its typed error on 200+{{error}}")
        if probe_after is None or probe_after - probe_before < 1:
            failures.append(
                f"{short}: {ERROR_METRIC} did not increment for its 200+{{error}} request "
                f"(before={probe_before_raw}, after={probe_after})"
            )

    # If no probe could actually run against a real SDK, this is BLOCKED, not a pass — we refuse to
    # manufacture a green (AGENTS.md: a gate that can't fail is worse than no gate).
    if completed_probes == 0:
        return Result(
            scenario=META["name"], status=Status.BLOCKED, seeded_from=META["seeded_from"],
            why="no certified SDK probe ran against the candidate",
            evidence=evidence,
        )

    # Metric assertion (ties the observability gate).
    after = None
    for attempt in range(10):
        after = scrape_metric(ctx.metrics_url, ERROR_METRIC)
        if after is not None and after - baseline >= erroring_calls:
            break
        if attempt < 9:
            time.sleep(1)
    evidence["metric_after"] = after
    metric_ok = False
    if after is None:
        failures.append(
            f"{ERROR_METRIC} not exposed (before={before}, after={after}) — in-band error metric "
            "is not wired (server#2243)"
        )
    elif after - baseline < erroring_calls:
        failures.append(
            f"{ERROR_METRIC} only rose by {after - baseline}, expected >= {erroring_calls}"
        )
    else:
        metric_ok = True
    evidence["metric_incremented"] = metric_ok

    if failures:
        return Result(
            scenario=META["name"], status=Status.FAIL, seeded_from=META["seeded_from"],
            why="; ".join(failures), evidence=evidence,
        )
    return Result(
        scenario=META["name"], status=Status.PASS, seeded_from=META["seeded_from"],
        why=f"all three frozen SDKs raised typed errors; {ERROR_METRIC} incremented by at least {erroring_calls}",
        evidence=evidence,
    )
