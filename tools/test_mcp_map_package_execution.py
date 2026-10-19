"""Authored composition and fresh-resource semantics join the required floor."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_release_owned_map_package_oracle():
    result = subprocess.run(["python3", "certification/installed-clients/mcp_map_package_validation_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_owned_javascript_map_package_journey():
    # The real HTTP observer/source-tamper suite completed successfully twice
    # in 121–122 seconds during exact-head review; keep bounded headroom.
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_map_package_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
