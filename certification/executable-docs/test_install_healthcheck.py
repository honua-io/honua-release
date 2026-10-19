"""Check the two customer Compose probes after real Compose and shell expansion."""

import io
import json
import os
import re
import subprocess
import tarfile
import uuid
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


def _created_healthcheck(directory: Path) -> str:
    # Use the real Compose container-create path, which resolves $$ before sending
    # Config.Healthcheck.Test to Docker. `compose config` serializes $$ for another
    # Compose read and is not the command a created container receives.
    tag = f"honua-execdocs-health-probe:{uuid.uuid4().hex}"
    project = f"execdocsprobe{uuid.uuid4().hex[:12]}"
    archive = directory / "scratch.tar"
    with tarfile.open(archive, "w") as tar:
        marker = tarfile.TarInfo("probe-marker")
        marker.size = 1
        tar.addfile(marker, io.BytesIO(b"x"))

    env = {
        "HOME": str(directory),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "HONUA_ADMIN_PASSWORD": DUMMY_KEY,
        "HONUA_MASTER_KEY": "a" * 64,
        "HONUA_SERVER_IMAGE": tag,
    }

    def docker(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *args], cwd=directory, env=env,
                              capture_output=True, text=True, timeout=30, check=False)

    imported = docker("import", "--change", 'CMD ["/probe-marker"]', str(archive), tag)
    assert imported.returncode == 0, "could not import the local scratch probe image"
    compose = ("compose", "--project-name", project, "-f", "compose.yaml")
    try:
        created = docker(*compose, "create", "--no-deps", "--pull", "never", "honua")
        assert created.returncode == 0, "Compose could not create the documented Honua service"
        container = docker(*compose, "ps", "-aq", "honua")
        assert container.returncode == 0 and container.stdout.strip(), "Compose created no Honua container"
        inspected = docker("inspect", "--format", "{{json .Config.Healthcheck.Test}}",
                           container.stdout.strip())
        assert inspected.returncode == 0, "could not inspect the created healthcheck"
        command_type, command = json.loads(inspected.stdout)
        assert command_type == "CMD-SHELL"
        return command
    finally:
        docker(*compose, "down", "--remove-orphans")
        docker("image", "rm", "-f", tag)


@pytest.mark.parametrize("guide", GUIDES)
def test_install_probe_expands_admin_key_after_compose(guide: str, tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(_compose_example(ROOT / guide), encoding="utf-8")
    command = _created_healthcheck(tmp_path)
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
