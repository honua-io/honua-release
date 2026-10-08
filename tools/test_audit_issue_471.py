from pathlib import Path
import json
import os
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_rel_001_dependency_security_receipts_are_not_excluded_without_coverage():
    policy = yaml.safe_load((ROOT / "certification/security-checks.yaml").read_text())
    assert policy == {}


def test_rel_002_image_gates_bind_scans_to_manifest_digest():
    for name in ("gate-security.yml", "gate-sbom.yml"):
        workflow = (ROOT / ".github/workflows" / name).read_text()
        if name == "gate-security.yml":
            assert "component: [honua-server, honua-console]" in workflow
            assert "architecture: [amd64, arm64]" in workflow
            assert "c['platformDigests']['${{ matrix.architecture }}']" in workflow
        else:
            assert "architecture: [amd64, arm64]" in workflow
            assert "c['platformDigests']['${{ matrix.architecture }}']" in workflow


def test_rel_003_promotion_passes_minting_time_to_freshness_check():
    workflow = (ROOT / ".github/workflows/promote.yml").read_text()
    assert '--certification-time "${{ steps.train.outputs.updated_at }}"' in workflow


def test_rel_004_security_findings_is_a_required_train_gate():
    workflow = (ROOT / ".github/workflows/release-train.yml").read_text()
    assert "gate_security_findings:" in workflow
    assert "security-findings|$S_SECURITY_FINDINGS" in workflow


def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def _step(workflow, job, name):
    return next(s for s in _workflow(workflow)["jobs"][job]["steps"] if s.get("name") == name)


def _run(script, tmp_path, env=None):
    return subprocess.run(["bash", "-c", script], cwd=tmp_path,
                          env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=15)


def _fragment_names(workflow):
    if workflow == "gate-security.yml":
        return [f"security-image-{c}-{a}" for c in ("honua-server", "honua-console")
                for a in ("amd64", "arm64")] + ["security-repo"]
    return ["sbom-platform", "sbom-server-image-amd64", "sbom-server-image-arm64"]


@pytest.mark.parametrize("workflow", ["gate-security.yml", "gate-sbom.yml"])
@pytest.mark.parametrize("problem", ["none", "missing", "duplicate", "failed-producer"])
@pytest.mark.parametrize("enforcement", ["strict", "bootstrap"])
def test_scan_reports_cannot_pass_partial_duplicate_or_failed_coverage(tmp_path, workflow, problem, enforcement):
    names = _fragment_names(workflow)
    fragments = tmp_path / "fragments"
    fragments.mkdir()
    selected = names[:-1] if problem == "missing" else names
    if problem == "duplicate":
        selected = selected + [names[0]]
    for i, name in enumerate(selected):
        (fragments / f"{i}.json").write_text(json.dumps({"gate": name, "status": "pass", "why": "tested"}))
    step = _step(workflow, "report", "Assemble gate-report.json")
    env = {"PLATFORM_LABEL": "2026.1-rc.1", "ENFORCEMENT": enforcement,
           "RUN_URL": "https://example.test/run/1", "GITHUB_OUTPUT": str(tmp_path / "output"),
           "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
           "IMAGE_RESULT": "failure" if problem == "failed-producer" else "success",
           "OTHER_RESULT": "success"}
    result = _run(step["run"], tmp_path, env)
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "out/gate-report.json").read_text())
    assert (report["overallStatus"] == "pass") == (problem == "none"), report


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_sbom_scans_each_exact_child_with_explicit_platform(tmp_path, architecture):
    """Execute the workflow command boundary without pulling or scanning a real image."""
    image = "ghcr.io/honua-io/honua-server@sha256:" + "b" * 64
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.jsonl"
    script = f'''#!{sys.executable}
import json, os, pathlib, sys
with open(os.environ["CALLS"], "a") as output:
    output.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
if pathlib.Path(sys.argv[0]).name == "syft":
    for arg in sys.argv[1:]:
        if arg.startswith(("cyclonedx-json=", "spdx-json=")):
            pathlib.Path(arg.split("=", 1)[1]).write_text(json.dumps({{"components": [{{"name": "example"}}]}}))
'''
    for command in ("docker", "syft"):
        executable = bindir / command
        executable.write_text(script)
        executable.chmod(0o755)
    step = _step("gate-sbom.yml", "image-sbom", "SBOM the server image (CycloneDX + SPDX)")
    run = step["run"].replace("${{ steps.img.outputs.image }}", image).replace("${{ matrix.architecture }}", architecture)
    result = _run(run, tmp_path, {"PATH": f"{bindir}:{os.environ['PATH']}", "CALLS": str(calls),
                                "GH_TOKEN": "test-only", "GITHUB_ENV": str(tmp_path / "env")})
    assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    pull = next(args for args in recorded if args[:2] == ["docker", "pull"])
    scan = next(args for args in recorded if args[0] == "syft")
    for command in (pull, scan):
        assert command[command.index("--platform") + 1] == f"linux/{architecture}"
        assert image in command
    assert "status=pass" in (tmp_path / "env").read_text()


def test_findings_permissions_are_available_through_both_train_callers():
    required = _workflow("gate-security-findings.yml")["permissions"]
    train = _workflow("release-train.yml")["permissions"]
    nightly = _workflow("nightly-certification.yml")["jobs"]["train"]["permissions"]
    for caller in (train, nightly):
        for scope, level in required.items():
            assert caller.get(scope) in (level, "write"), f"missing caller permission: {scope}"
