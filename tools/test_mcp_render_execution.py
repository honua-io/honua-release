"""Release-owned render semantics and bootstrap controls join the full floor."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_release_owned_render_oracle_and_bootstrap():
    result = subprocess.run(["python3", "certification/installed-clients/mcp_render_validation_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_owned_javascript_render_scenario():
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_render_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
