"""The required floor retains discriminating authored publication controls."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_release_owned_ingest_publish_semantics():
    result = subprocess.run(["python3", "-B", "certification/installed-clients/mcp_ingest_publish_validation_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_owned_ingest_publish_source_and_rejudge_binding():
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_ingest_publish_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
