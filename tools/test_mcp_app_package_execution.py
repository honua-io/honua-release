"""Authored original app rows join the maintained release source-control floor."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_release_owned_app_package_semantics():
    result = subprocess.run(["python3", "certification/installed-clients/mcp_app_package_validation_controls.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_owned_app_package_actual_observer_and_proof_binding():
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_app_package_validation.test.mjs"],
        cwd=ROOT, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
