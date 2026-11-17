"""The DR drills must run against a server image that ships no shell.

From honua-server 532565bc on, both the native-AOT and the JIT server images are chiseled: they
carry `/app/Honua.Server` (or `dotnet`) and the compiled `/app/honua-healthcheck` probe, but no
`/bin/sh`, `bash`, `tar` or `chown`. The scoped restore drill (run.sh) failed to start its server
with `exec: "/bin/sh": stat /bin/sh: no such file or directory` because the compose file wrapped the
entrypoint in a shell, and every shell-based healthcheck or volume helper on the server image
fails the same way. These tests pin the drill topologies to the image's own tooling and keep the
PostgreSQL cold-start guarantee the removed `sleep 5` used to approximate.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SEAM = ROOT / "e2e/local-docker/docker-compose.yml"
FULL = ROOT / "e2e/dr-drill/compose.full-platform.yml"
JOURNEY = ROOT / "certification/terminal-journey/targets/build-compose.yml"
CANDIDATE = ROOT / "e2e/harness/compose.candidate.yml"
CONSOLE_S4 = ROOT / "e2e/harness/compose.console-s4.yml"
SHELLS = ("sh", "/bin/sh", "bash", "/bin/bash", "/usr/bin/bash", "CMD-SHELL")
SERVER_IMAGE_VAR = "${HONUA_SERVER_IMAGE"


def _services(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"]


def _argv(value) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else [str(item) for item in value]


def _uses_shell(argv: list[str]) -> bool:
    return any(token in SHELLS or token.startswith(("sh ", "bash ", "/bin/sh ")) for token in argv)


@pytest.mark.parametrize("compose", [SEAM, FULL, JOURNEY, CANDIDATE, CONSOLE_S4],
                         ids=["seam", "full-platform", "terminal-journey", "harness-candidate", "console-s4"])
def test_no_service_runs_a_shell_on_the_server_image(compose):
    for name, service in _services(compose).items():
        if not str(service.get("image", "")).startswith(SERVER_IMAGE_VAR):
            continue
        assert not _uses_shell(_argv(service.get("entrypoint"))), f"{name} entrypoint needs a shell"
        assert not _uses_shell(_argv(service.get("command"))), f"{name} command needs a shell"
        test = _argv((service.get("healthcheck") or {}).get("test"))
        assert not _uses_shell(test), f"{name} healthcheck needs a shell"


@pytest.mark.parametrize("compose", [SEAM, FULL], ids=["seam", "full-platform"])
def test_server_readiness_uses_the_images_compiled_probe(compose):
    server = _services(compose)["server"]
    assert "entrypoint" not in server, "the server must start through the image's own entrypoint"
    assert server["healthcheck"]["test"] == ["CMD", "/app/honua-healthcheck", "--ready"]


@pytest.mark.parametrize("compose", [SEAM, FULL], ids=["seam", "full-platform"])
def test_postgres_is_healthy_only_after_the_init_restart(compose):
    # The server's one-shot migration used to race the PostGIS init server, hidden by `sleep 5`
    # inside a server shell. Healthy now means PID 1 is the final postmaster and it accepts TCP.
    db = _services(compose)["db"]
    test = " ".join(_argv(db["healthcheck"]["test"]))
    assert "/var/lib/postgresql/data/postmaster.pid" in test
    assert '= "1"' in test
    assert "pg_isready -h 127.0.0.1 -U honua -d honua" in test
    depends = _services(compose)["server"]["depends_on"]
    assert depends["db"]["condition"] == "service_healthy"


def test_storage_init_uses_the_pinned_store_image_not_the_server():
    services = _services(FULL)
    init = services["storage-init"]
    assert init["image"] == services["db"]["image"]
    assert init["user"] == "0:0"
    assert "chown -R 1001:1001 /var/lib/honua/storage" in " ".join(_argv(init["command"]))


def _full_platform():
    sys.path.insert(0, str(ROOT / "e2e"))
    sys.path.insert(0, str(ROOT / "e2e/dr-drill"))
    spec = importlib.util.spec_from_file_location("dr_full_platform_shellless",
                                                  ROOT / "e2e/dr-drill/full_platform.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Proc:
    returncode = 0
    stdout = b""
    stderr = b""


def test_volume_helpers_never_run_a_shell_in_the_server_image(monkeypatch):
    module = _full_platform()
    calls: list[tuple] = []
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: calls.append(args) or _Proc())

    class StackStub:
        image = "ghcr.io/honua-io/honua-server@sha256:" + "d" * 64

        def volume(self, name):
            return f"proj_{name}"

    stack = StackStub()
    module.volume_helper(stack, "honua_storage", "/storage", "tar -C /storage -cf - .")
    module.Stack.volume_is_empty(stack, "honua_storage", "/storage")
    assert len(calls) == 2
    helper = _services(FULL)["db"]["image"]
    for args in calls:
        assert stack.image not in args, "a shell snippet was sent to the shell-less server image"
        assert helper in args
        assert args[args.index("--entrypoint") + 1] == "/bin/sh"


def test_terminal_journey_replica_starts_like_the_server_and_inherits_the_db_gate():
    services = _services(JOURNEY)
    replica = services["replica"]
    assert "entrypoint" not in replica, "the replica must start through the image's own entrypoint"
    assert replica["healthcheck"]["test"] == ["CMD", "/app/honua-healthcheck", "--ready"]
    # The db service extends the seam's db; overriding its healthcheck would drop the post-restart
    # postmaster check that replaced the server's `sleep 5`.
    assert "healthcheck" not in services["db"]
