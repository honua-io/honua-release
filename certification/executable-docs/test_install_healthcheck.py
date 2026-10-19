"""Check the two customer Compose probes after real Compose and shell expansion."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
GUIDES = ("docs/INSTALL-2026.1.md", "docs/CUSTOMER-INSTALL.md")
DUMMY_KEY = "dummy-install-healthcheck-key"


def _compose_example(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"<!-- doc-run: file=compose\.yaml -->\s*```yaml\n(.*?)\n```", text, re.DOTALL)
    assert match is not None, f"{path.name}: missing executable Compose example"
    return match.group(1) + "\n"


def _run_probe(command: str, directory: Path) -> list[str]:
    # A local wget stand-in observes argv; the probe cannot reach a server or print a credential.
    stub = directory / "wget"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['PROBE_CAPTURE'], 'w', encoding='utf-8') as out:\n"
        "    json.dump(sys.argv[1:], out)\n"
        "print('{\"mode\":\"disabled\"}')\n",
        encoding="utf-8",
    )
    stub.chmod(0o700)
    capture = directory / "probe-argv.json"
    env = {
        "HOME": str(directory),
        "PATH": f"{directory}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "LANG": "C",
        "HONUA_ADMIN_PASSWORD": DUMMY_KEY,
        "PROBE_CAPTURE": str(capture),
    }
    result = subprocess.run(["sh", "-c", command], cwd=directory, env=env,
                            capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0, "health probe did not accept the dummy endpoint response"
    return json.loads(capture.read_text(encoding="utf-8"))


@pytest.mark.parametrize("guide", GUIDES)
def test_install_probe_expands_admin_key_after_compose(guide: str, tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(_compose_example(ROOT / guide), encoding="utf-8")
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "HONUA_ADMIN_PASSWORD": DUMMY_KEY,
        "HONUA_MASTER_KEY": "a" * 64,
        "HONUA_SERVER_IMAGE": "example.invalid/honua:offline-compose-config",
    }
    rendered = subprocess.run(["docker", "compose", "-f", "compose.yaml", "config", "--format", "json"],
                              cwd=tmp_path, env=env, capture_output=True, text=True,
                              timeout=15, check=False)
    assert rendered.returncode == 0, "Compose did not render the documented install example"
    config = json.loads(rendered.stdout)
    command_type, command = config["services"]["honua"]["healthcheck"]["test"]
    assert command_type == "CMD-SHELL"
    argv = _run_probe(command, tmp_path)
    assert argv.count(f"--header=X-API-Key: {DUMMY_KEY}") == 1
    assert not any("${HONUA_ADMIN_PASSWORD}" in arg for arg in argv)

    # The former single-quoted shell header must remain a failing control.
    old_command = command.replace(
        '"X-API-Key: ${HONUA_ADMIN_PASSWORD}"',
        "'X-API-Key: ${HONUA_ADMIN_PASSWORD}'",
    )
    assert old_command != command
    old_argv = _run_probe(old_command, tmp_path)
    assert f"--header=X-API-Key: {DUMMY_KEY}" not in old_argv
    assert "--header=X-API-Key: ${HONUA_ADMIN_PASSWORD}" in old_argv
