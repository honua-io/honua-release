"""Required floor selects the canonical style producer/oracle controls."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_release_owned_style_semantics():
    result = subprocess.run(["python3", "-B", "certification/installed-clients/mcp_style_validation_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_owned_style_source_and_rejudge_binding():
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_style_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
