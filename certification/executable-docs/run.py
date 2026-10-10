#!/usr/bin/env python3
"""Run every getting-started block of the public docs against the booted release candidate.

    python certification/executable-docs/run.py --boot --evidence-uri <run url>

For each document in sources.json (read at its release revision, see inventory.py) the runner
starts clean containers from digest-pinned node / python / dotnet images, points npm, pip and NuGet
at the registry guard (only manifest-pinned Honua packages are installable), and executes the
document's blocks in order against the candidate `image@digest` booted by e2e/harness/boot.sh with
licensing disabled. Result per block: pass, fail (with the stdout/stderr tail), needs-input, or
blocked (a failing block the document marks `doc-run: blocked <issue>`). A
document is red when any block fails; the run is red when any document is.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from blocks import Block, extract  # noqa: E402
from inputs import assigned_names, doc_id, load_vars, needs, render  # noqa: E402
from inventory import InventoryError, Resolver, document_record, drift  # noqa: E402
from registry_guard import pins_from_manifest, NPM_HONUA, pypi_normalize  # noqa: E402

TAIL = 2000
SERVE = re.compile(
    r"^\s*(?:npm\s+(?:run\s+)?(?:dev|start|serve|preview)|pnpm\s+(?:run\s+)?(?:dev|start)|yarn\s+(?:dev|start)|"
    r"npx\s+(?:-y\s+)?(?:vite|serve|http-server)|vite\b|python3?\s+-m\s+http\.server|uvicorn\b|flask\s+run|"
    r"dotnet\s+watch\b|docker\s+compose\s+up(?!.*\s-d\b)(?!.*--detach))", re.M)
DEFAULT_TIMEOUT = 600
SERVE_WINDOW = 60
SECRETISH = re.compile(
    r"(([Pp]ass(word|wd)?|PASSWORD|[Ss]ecret|SECRET|[Tt]oken|TOKEN|[Aa]pi[-_]?[Kk]ey|API[-_]?KEY|MASTER_KEY)"
    r"[\"']?\s*[=:]\s*[\"']?)(?!os\.environ|os\.getenv|process\.env|Environment\.|\$|\*\*\*)[^\s\"';,]+")


class RunError(RuntimeError):
    pass


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return SECRETISH.sub(r"\1***", text)


def tail(text: str, limit: int = TAIL) -> str:
    """The first error line usually explains the failure and the end shows where it stopped."""
    if len(text) <= limit:
        return text
    head = text[: limit // 4]
    first_error = re.search(r"^.*(?:Error|error|ERR!|Exception|FAIL)[^\n]*$", text, re.M)
    if first_error and first_error.start() > len(head):
        head += "\n…\n" + first_error.group(0)[:300]
    return head + "\n…\n" + text[-(limit - len(head)):]


# ── oracle ───────────────────────────────────────────────────────────────────────────────────────

def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def assert_output(expected: str, actual: str) -> tuple[bool, str]:
    """Compare complete output, normalizing whitespace; only explicit ellipses elide values."""
    try:
        want = json.loads(expected)
        got = json.loads(actual)
        def matches(want: Any, got: Any) -> bool:
            if isinstance(want, str) and want in {"...", "…"}:
                return True
            if isinstance(want, dict):
                return isinstance(got, dict) and want.keys() == got.keys() and all(matches(v, got[k]) for k, v in want.items())
            if isinstance(want, list):
                return isinstance(got, list) and len(want) == len(got) and all(
                    matches(a, b) for a, b in zip(want, got))
            return type(want) is type(got) and want == got

        ok = matches(want, got)
        return ok, "expected JSON values matched" if ok else "output differs from documented JSON values"
    except (json.JSONDecodeError, IndexError):
        pass
    pattern = ".*?".join(re.escape(part) for part in re.split(r"…|\.\.\.", _norm(expected)))
    ok = re.fullmatch(pattern, _norm(actual)) is not None
    return ok, "normalized full output matched" if ok else "output differs from documented full output"


def exception_outcome() -> Outcome:
    return Outcome("fail", "unexpected runner exception", stderr=tail(traceback.format_exc()))


# ── program combination for fragments that continue an earlier block ───────────────────────────

CS_USING = re.compile(r"^\s*(global\s+)?using\s+(static\s+)?[\w.]+(\s*=\s*[\w.<>]+)?\s*;\s*$")
CS_TYPE = re.compile(r"^(public\s+|internal\s+|sealed\s+|static\s+|abstract\s+|partial\s+|file\s+)*"
                     r"(class|record|struct|interface|enum)\b")


def split_csharp(code: str) -> tuple[list[str], list[str], list[str]]:
    usings, statements, types = [], [], []
    lines = code.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if CS_USING.match(line):
            usings.append(line.strip())
        elif CS_TYPE.match(line) or line.startswith("namespace "):
            depth, opened = 0, False
            while i < len(lines):
                types.append(lines[i])
                depth += lines[i].count("{") - lines[i].count("}")
                opened = opened or "{" in lines[i]
                if (opened and depth <= 0) or (not opened and lines[i].rstrip().endswith(";")):
                    break
                i += 1
        else:
            statements.append(line)
        i += 1
    return usings, statements, types


def combine_csharp(codes: list[str]) -> str:
    usings: list[str] = []
    statements: list[str] = []
    types: list[str] = []
    for code in codes:
        u, s, t = split_csharp(code)
        usings += [x for x in u if x not in usings]
        statements += s
        types += t
    return "\n".join(usings + [""] + statements + [""] + types) + "\n"


JS_IMPORT = re.compile(r"^\s*import\s[^;]*?from\s+['\"][^'\"]+['\"]\s*;?\s*$|^\s*import\s+['\"][^'\"]+['\"]\s*;?\s*$")


def combine_js(codes: list[str]) -> str:
    imports: list[str] = []
    body: list[str] = []
    for code in codes:
        for line in code.splitlines():
            if JS_IMPORT.match(line):
                if line.strip() not in imports:
                    imports.append(line.strip())
            else:
                body.append(line)
    return "\n".join(imports + body) + "\n"


def continuation_error(language: str, text: str) -> bool:
    if language == "csharp":
        return "CS0103" in text
    return bool(re.search(r"ReferenceError: \w+ is not defined|TS2304|TS2552", text))


# ── containers ───────────────────────────────────────────────────────────────────────────────────

def docker(*args: str, timeout: int = 600, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    if check and proc.returncode:
        raise RunError(f"docker {' '.join(args[:3])} failed: {proc.stderr.strip()[-500:]}")
    return proc


# A documented prerequisite tool -> the Debian packages a reader would install for it. `sudo` stands for
# "the reader can administer their machine" (for example `npx playwright install --with-deps`, which
# installs Chromium's system libraries through sudo): the host-UID reader gets a passwordless sudo.
APT_PREREQUISITES = {"jq": ["jq"], "python": ["python-is-python3"], "sudo": ["sudo"]}
LANGUAGE_RUNTIME = {"python": "python", "javascript": "node", "typescript": "node", "csharp": "dotnet"}


@dataclass
class Outcome:
    status: str
    detail: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration: float = 0.0
    mode: str | None = None
    command_failed: bool = False  # executor witnessed the block command fail, not Docker or the runner


@dataclass
class Session:
    name: str
    workdir: Path
    runtimes: dict[str, str]
    guard_base: str
    tools: Path
    docker_access: bool
    network: str
    run_id: str
    typescript: str
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    containers: dict[str, str] = field(default_factory=dict)
    baselines: dict[str, dict[str, str]] = field(default_factory=dict)
    passed: dict[str, list[str]] = field(default_factory=dict)
    py: subprocess.Popen | None = None
    py_lines: "queue.Queue[str]" = field(default_factory=queue.Queue)
    py_path: str | None = None
    paths: dict[str, str] = field(default_factory=dict)   # PATH per runtime (venv activation, npm -g)
    prerequisites: set[str] = field(default_factory=set)  # tools the session's documents say to have
    teardowns: list[tuple[dict[str, Any], str, str, dict[str, Any]]] = field(default_factory=list)
    dotnet_project: bool = False
    servers_seen: dict[str, dict[str, Any]] = field(default_factory=dict)
    counter: int = 0

    def __post_init__(self) -> None:
        self.state = self.workdir / ".docrun"
        self.state.mkdir(parents=True, exist_ok=True)
        (self.workdir / "app").mkdir(exist_ok=True)
        self.cwd = str(self.workdir / "app")
        nuget = self.state / "nuget"
        nuget.mkdir(exist_ok=True)
        self.home = self.state / "home"
        self.home.mkdir(exist_ok=True)
        (nuget / "NuGet.Config").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<configuration>\n  <packageSources>\n    <clear />\n'
            f'    <add key="release-pinned" value="{self.guard_base}/nuget/v3/index.json" allowInsecureConnections="true" />\n'
            "  </packageSources>\n</configuration>\n")
        home_nuget = self.home / ".nuget/NuGet"
        home_nuget.mkdir(parents=True, exist_ok=True)
        shutil.copy2(nuget / "NuGet.Config", home_nuget / "NuGet.Config")
        shutil.copy2(HERE / "pysession.py", self.state / "pysession.py")

    # container lifecycle
    def container(self, runtime: str) -> str:
        if runtime in self.containers:
            return self.containers[runtime]
        image = self.runtimes[runtime]
        name = f"execdocs-{self.run_id}-{re.sub(r'[^a-z0-9]+', '-', self.name.lower())[:40]}-{runtime}"
        g = self.guard_base
        args = ["run", "-d", "--name", name, "--network", self.network,
                "--user", f"{os.getuid()}:{os.getgid()}", "-e", f"HOME={self.home}",
                "-v", f"{self.workdir}:{self.workdir}", "-w", self.cwd,
                "-e", f"npm_config_registry={g}/npm/", "-e", "npm_config_update_notifier=false",
                "-e", "npm_config_fund=false", "-e", f"YARN_NPM_REGISTRY_SERVER={g}/npm/",
                "-e", f"npm_config_prefix={self.home}/.local",
                "-e", f"PIP_INDEX_URL={g}/pypi/simple/", "-e", f"UV_DEFAULT_INDEX={g}/pypi/simple/",
                "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1", "-e", "PIP_ROOT_USER_ACTION=ignore",
                "-e", "DOTNET_CLI_TELEMETRY_OPTOUT=1", "-e", "DOTNET_NOLOGO=1",
                "--label", f"honua.execdocs.run={self.run_id}"]
        path = f"{self.home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        if "node" in self.prerequisites and runtime != "node":
            args += ["-v", f"{self.tools / 'node'}:/opt/node:ro"]
            path = "/opt/node/bin:" + path
        if runtime == "dotnet":
            path += f":{self.home}/.dotnet/tools"
        if path != "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin":
            args += ["-e", f"PATH={path}"]
        if self.docker_access:
            args += ["--group-add", str(Path("/var/run/docker.sock").stat().st_gid),
                     "-v", "/var/run/docker.sock:/var/run/docker.sock",
                     "-v", f"{self.tools / 'docker'}:/usr/local/bin/docker:ro",
                     "-v", f"{self.tools / 'docker-compose'}:/usr/local/lib/docker/cli-plugins/docker-compose:ro"]
        args += [image, "sleep", "infinity"]
        docker("rm", "-f", name)
        docker(*args, check=True, timeout=1800)
        self.containers[runtime] = name
        packages = sorted({pkg for p in self.prerequisites for pkg in APT_PREREQUISITES.get(p, [])})
        if packages:   # a tool the document lists under its prerequisites, installed as the reader would
            script = ("apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                      + " ".join(packages))
            if "sudo" in self.prerequisites:
                uid, gid = os.getuid(), os.getgid()
                script += (f" && (getent group {gid} >/dev/null || groupadd -g {gid} reader)"
                           f" && (getent passwd {uid} >/dev/null || useradd -o -u {uid} -g {gid} -M"
                           f" -d {shlex.quote(str(self.home))} reader)"
                           f" && echo '#{uid} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/reader"
                           " && chmod 0440 /etc/sudoers.d/reader")
            setup = docker("exec", "--user", "0", name, "bash", "-c", script, timeout=900)
            if setup.returncode:
                raise RunError(f"could not install the documented prerequisites {packages}: {setup.stderr[-300:]}")
        base = docker("exec", name, "env", "-0").stdout
        self.baselines[runtime] = dict(item.split("=", 1) for item in base.split("\0") if "=" in item)
        return name

    def close(self) -> None:
        if self.py is not None:
            self.py.kill()
            self.py = None
        for name in self.containers.values():
            docker("rm", "-f", name)
        self.containers.clear()

    def exec_env(self, runtime: str | None = None) -> list[str]:
        out = []
        for key, value in self.env.items():
            out += ["-e", f"{key}={value}"]
        if runtime and runtime in self.paths:
            out += ["-e", f"PATH={self.paths[runtime]}"]
        return out

    def next_path(self, suffix: str, directory: str | None = None) -> Path:
        self.counter += 1
        base = Path(directory or self.state)
        return base / f"docrun-{self.counter:03d}{suffix}"

    def ensure_dotnet_project(self) -> None:
        """A .NET reader adds packages to a project of their own: start them in a fresh console app."""
        if self.dotnet_project:
            return
        outcome = self._exec(self.container("dotnet"), ["dotnet", "new", "console", "--name", "App",
                                                         "--output", self.cwd], DEFAULT_TIMEOUT)
        if outcome.status != "pass":
            raise RunError(f"could not create the reader's console project: {outcome.stderr[-300:]}")
        self.dotnet_project = True

    def put(self, runtime: str, path: Path, content: str) -> None:
        """Write into the reader's directories from inside the container: the reader owns them there
        (on a Linux host, files a root container created are not writable by the runner's user)."""
        proc = subprocess.run(["docker", "exec", "-i", self.container(runtime), "sh", "-c",
                               'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", str(path)],
                              input=content, text=True, capture_output=True, check=False)
        if proc.returncode:
            raise RunError(f"could not write {path}: {proc.stderr.strip()[-300:]}")

    # executors
    def run_shell(self, code: str, runtime: str, timeout: int, serve: bool,
                  readiness: dict[str, str] | None = None) -> Outcome:
        name = self.container(runtime)
        script = self.next_path(".sh")
        script.write_text(code)
        out, err, envfile, cwdfile = (script.with_suffix(s) for s in (".out", ".err", ".env", ".cwd"))
        wrapper = (
            f"cd {shlex.quote(self.cwd)} 2>/dev/null || cd {shlex.quote(str(self.workdir))}\n"
            f"trap 'rc=$?; pwd > {shlex.quote(str(cwdfile))}; env -0 > {shlex.quote(str(envfile))}; exit $rc' EXIT\n"
            f"set -a -e\n. {shlex.quote(str(script))}\n")
        window = SERVE_WINDOW if serve else timeout
        started = time.monotonic()
        with out.open("w") as stdout_file, err.open("w") as stderr_file:
            proc = subprocess.Popen(
                ["docker", "exec", *self.exec_env(runtime), name, "timeout", "-k", "5", str(window), "bash", "-c",
                 f"{wrapper}", "docrun"],
                stdin=subprocess.DEVNULL, stdout=stdout_file, stderr=stderr_file)
        ready = False
        deadline = started + window + 60
        while proc.poll() is None and time.monotonic() < deadline:
            if serve and readiness and not ready:   # once observed, stays observed: the server stops at the window's end
                if readiness.get("url"):
                    probe = docker("exec", name, "curl", "-fsS", "--max-time", "2", readiness["url"], timeout=10)
                    ready = probe.returncode == 0
                elif readiness.get("log"):
                    ready = readiness["log"] in (out.read_text(errors="replace") + err.read_text(errors="replace")).splitlines()
            time.sleep(0.2)
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        duration = time.monotonic() - started
        stdout, stderr = out.read_text(errors="replace"), err.read_text(errors="replace")
        if cwdfile.exists():
            self.cwd = cwdfile.read_text().strip() or self.cwd
        if envfile.exists():
            current = dict(i.split("=", 1) for i in envfile.read_text(errors="replace").split("\0") if "=" in i)
            baseline = self.baselines.get(runtime, {})
            if current.get("PATH") and current["PATH"] != baseline.get("PATH"):
                self.paths[runtime] = current["PATH"]
            for key, value in current.items():
                if key in {"PWD", "OLDPWD", "SHLVL", "_", "HOME", "PATH", "HOSTNAME"} or key.startswith("BASH_"):
                    continue
                if baseline.get(key) != value:
                    self.env[key] = value
        code_ = proc.returncode
        if code_ == 124:
            if serve:
                status = "pass" if ready else ("fail" if readiness else "needs-input")
                detail = ("documented readiness evidence observed" if ready else
                          "documented readiness evidence was not observed" if readiness else
                          "long-running command needs a documented readiness URL or expected log line")
                return Outcome(status, detail,
                               stdout, stderr, code_, duration, "serve", command_failed=envfile.exists())
            return Outcome("fail", f"timed out after {window}s (waiting for input or a process that never ends)",
                           stdout, stderr, code_, duration, command_failed=envfile.exists())
        if code_:
            return Outcome("fail", f"exit code {code_}", stdout, stderr, code_, duration, command_failed=envfile.exists())
        if serve:
            return Outcome("fail", "long-running command exited before readiness could be established",
                           stdout, stderr, 0, duration, "serve")
        return Outcome("pass", "exit code 0", stdout, stderr, 0, duration)

    def _py_reader(self) -> None:
        assert self.py is not None and self.py.stdout is not None
        for line in self.py.stdout:
            self.py_lines.put(line)

    def run_python(self, code: str, timeout: int) -> Outcome:
        name = self.container("python")
        if self.py is not None and self.paths.get("python") != self.py_path:
            self.py.kill()   # the reader activated a virtualenv: their next REPL is that interpreter
            self.py = None
        if self.py is None or self.py.poll() is not None:
            self.py_lines = queue.Queue()
            self.py_path = self.paths.get("python")
            self.py = subprocess.Popen(["docker", "exec", "-i", *self.exec_env("python"), name, "python3", "-u",
                                        str(self.state / "pysession.py")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       text=True)
            threading.Thread(target=self._py_reader, daemon=True).start()
        path = self.next_path(".py")
        path.write_text(code)
        request = {"file": str(path), "cwd": self.cwd, "env": self.env}
        started = time.monotonic()
        assert self.py.stdin is not None
        self.py.stdin.write(json.dumps(request) + "\n")
        self.py.stdin.flush()
        try:
            answer = json.loads(self.py_lines.get(timeout=timeout))
        except queue.Empty:
            self.py.kill()
            docker("exec", name, "pkill", "-f", "pysession.py")
            self.py = None
            return Outcome("fail", f"timed out after {timeout}s", _read(path, ".out"), _read(path, ".err"), None,
                           time.monotonic() - started)
        duration = time.monotonic() - started
        stdout, stderr = _read(path, ".out"), _read(path, ".err")
        if answer["ok"]:
            return Outcome("pass", "completed without an exception", stdout, stderr, 0, duration)
        last = [l for l in answer["error"].strip().splitlines() if l.strip()]
        return Outcome("fail", last[-1][:300] if last else "exception", stdout, stderr, 1, duration, command_failed=True)

    def run_js(self, code: str, language: str, timeout: int) -> Outcome:
        name = self.container("node")
        uses_require = "require(" in code and not re.search(r"^\s*import\s", code, re.M)
        suffix = ".cjs" if uses_require else (".mts" if language == "typescript" else ".mjs")
        path = self.next_path(suffix, self.cwd)
        self.put("node", path, code)
        flags = ["--experimental-transform-types", "--no-warnings"] if suffix == ".mts" else []
        return self._exec(name, ["node", *flags, path.name], timeout)

    def run_compile(self, code: str, timeout: int) -> Outcome:
        name = self.container("node")
        path = self.next_path(".mts", self.cwd)
        self.put("node", path, code)
        cmd = ["npx", "--yes", "-p", f"typescript@{self.typescript}", "tsc", "--noEmit", "--strict",
               "--skipLibCheck", "--target", "es2022", "--module", "nodenext", "--moduleResolution", "nodenext",
               "--lib", "esnext,dom,dom.iterable", path.name]
        outcome = self._exec(name, cmd, timeout)
        outcome.mode = "typecheck"
        return outcome

    def run_csharp(self, code: str, packages: list[tuple[str, str | None]], timeout: int) -> Outcome:
        name = self.container("dotnet")
        projects = sorted(Path(self.cwd).glob("*.csproj")) if Path(self.cwd).is_dir() else []
        if len(projects) == 1:
            project_dir, mode = Path(self.cwd), "reader-project"
        else:
            self.counter += 1
            project_dir, mode = self.state / f"cs-{self.counter:03d}", "scaffolded-console"
            setup = [f"dotnet new console --force -o {shlex.quote(str(project_dir))} >/dev/null"]
            for package, version in packages:
                pinned = f" --version {shlex.quote(version)}" if version else ""
                setup.append(f"dotnet add {shlex.quote(str(project_dir))} package {shlex.quote(package)}{pinned}")
            prep = self._exec(name, ["bash", "-c", " && ".join(setup)], timeout)
            if prep.status != "pass":
                prep.command_failed = False  # runner scaffolding is not the documented C# command
                prep.detail = "could not create a console project with the doc's packages: " + prep.detail
                return prep
        self.put("dotnet", project_dir / "Program.cs", code)
        outcome = self._exec(name, ["dotnet", "run", "--project", str(project_dir)], timeout)
        outcome.mode = mode
        return outcome

    def run_http(self, code: str, runtime: str, base_url: str, timeout: int) -> Outcome:
        lines = code.splitlines()
        first = lines[0].split() if lines else []
        if len(first) < 2:
            return Outcome("fail", "http block has no request line")
        method, target = first[0], first[1]
        headers, body, i = [], [], 1
        while i < len(lines) and lines[i].strip():
            headers.append(lines[i].strip())
            i += 1
        body = "\n".join(lines[i + 1:]).strip()
        if not re.match(r"https?://", target):
            host = next((h.split(":", 1)[1].strip() for h in headers if h.lower().startswith("host:")), None)
            target = (f"http://{host}" if host else base_url.rstrip("/")) + target
        cmd = ["curl", "-sS", "--fail-with-body", "-X", method, target]
        for header in headers:
            if not header.lower().startswith("host:"):
                cmd += ["-H", header]
        if body:
            cmd += ["--data-binary", body]
        return self._exec(self.container(runtime), cmd, timeout)

    def installed_honua(self, runtime: str) -> dict[str, str]:
        """Honua packages the reader's environment now has: pip site-packages and ./node_modules."""
        if runtime not in self.containers:
            return {}
        name = self.containers[runtime]
        found: dict[str, str] = {}
        if runtime == "python" or "python" in self.runtimes.get(runtime, ""):
            metadata = "import importlib.metadata as m,json; print(json.dumps([{\"name\":d.metadata[\"Name\"],\"version\":d.version} for d in m.distributions()]))"
            out = docker("exec", *self.exec_env(runtime), name, "python3", "-c", metadata, check=True).stdout
            for row in json.loads(out):
                if row["name"].lower().startswith("honua"):
                    found[pypi_normalize(row["name"])] = row["version"]
        modules = [Path(self.cwd) / "node_modules", self.home / ".local/lib/node_modules"]
        for package_json in (p for root in modules for p in root.rglob("package.json")):
            try:
                meta = json.loads(package_json.read_text())
                if NPM_HONUA.match(meta["name"]):
                    found[meta["name"]] = meta["version"]
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        if runtime == "dotnet":
            for assets in self.workdir.rglob("project.assets.json"):
                for library, meta in json.loads(assets.read_text()).get("libraries", {}).items():
                    package, version = library.rsplit("/", 1)
                    if meta.get("type") == "package" and package.lower().startswith("honua."):
                        found[package.lower()] = version
        return found

    def _exec(self, name: str, cmd: list[str], timeout: int) -> Outcome:
        started = time.monotonic()
        out, err = self.next_path(".out"), self.next_path(".err")
        runtime = next((key for key, value in self.containers.items() if value == name), None)
        exitfile = out.with_suffix(".exit")
        wrapper = f'"$@"; rc=$?; echo "$rc" > {shlex.quote(str(exitfile))}; exit "$rc"'
        proc = subprocess.run(["docker", "exec", "-w", self.cwd, *self.exec_env(runtime), name,
                               "bash", "-c", wrapper, "docrun", "timeout", "-k", "5", str(timeout), *cmd],
                              stdin=subprocess.DEVNULL, stdout=open(out, "w"), stderr=open(err, "w"),
                              timeout=timeout + 60, check=False)
        stdout, stderr = out.read_text(errors="replace"), err.read_text(errors="replace")
        duration = time.monotonic() - started
        if proc.returncode == 124:
            return Outcome("fail", f"timed out after {timeout}s", stdout, stderr, 124, duration, command_failed=exitfile.exists())
        if proc.returncode:
            return Outcome("fail", f"exit code {proc.returncode}", stdout, stderr, proc.returncode, duration, command_failed=exitfile.exists())
        return Outcome("pass", "exit code 0", stdout, stderr, 0, duration)


def _read(path: Path, suffix: str) -> str:
    target = Path(str(path) + suffix)
    return target.read_text(errors="replace") if target.exists() else ""


DOTNET_ADD = re.compile(r"dotnet\s+add\s+(?:\S+\s+)?package\s+([\w.]+)(?:\s+(?:--version|-v)\s+([\w.\-+*]+))?")


UNRESOLVED = re.compile(r"\{[a-zA-Z]+\.[A-Za-z0-9_]+\}")


def substitute(code: str, table: dict[str, str]) -> str:
    """Replace each literal in the executed code, longest first (so `...:2026.1-rc` wins over `...:2026.1`).

    Only the code a block runs is substituted; the report's `command` and the published page keep the
    document's own text (e.g. the release-channel tag the reader sees)."""
    for literal, value in sorted(table.items(), key=lambda kv: -len(kv[0])):
        code = code.replace(literal, value)
    return code


# ── documents ────────────────────────────────────────────────────────────────────────────────────

def snapshot_containers() -> set[str]:
    return set(docker("ps", "-aq", "--no-trunc").stdout.split())


def observe_servers(ids: set[str], seen: dict[str, dict[str, Any]], candidate_digest: str) -> None:
    """Record, while they exist, which server image each container a document started is running."""
    for cid in sorted(ids - set(seen)):
        info = docker("inspect", "-f", "{{.Config.Image}}|{{.Image}}|{{index .Config.Labels \"com.docker.compose.service\"}}", cid)
        if info.returncode:
            continue
        image, image_id, service = (info.stdout.strip().split("|") + ["", "", ""])[:3]
        if "honua-server" not in image and "honuaio/honua" not in image:
            seen[cid] = {}
            continue
        digests = docker("image", "inspect", "-f", "{{json .RepoDigests}}", image_id).stdout.strip()
        seen[cid] = {"container": cid[:12], "service": service, "image": image,
                     "isCandidate": candidate_digest in image or candidate_digest in digests}


def server_container_check(seen: dict[str, dict[str, Any]], candidate_digest: str) -> dict[str, Any] | None:
    """Did the stack a document started run the candidate server image?"""
    servers = [s for s in seen.values() if s]
    if not servers:
        return None
    ok = all(s["isCandidate"] for s in servers)
    detail = "; ".join(f"{s['service'] or s['container']} ran {s['image']}" for s in servers)
    return {"check": "boots-candidate-image", "status": "pass" if ok else "fail",
            "detail": (detail + (" (the candidate)" if ok else f" — not the candidate digest {candidate_digest}"))}


def cleanup_containers(ids: set[str]) -> None:
    """Remove what a session's documents started, with their compose volumes and networks."""
    projects = set()
    for cid in ids:
        label = docker("inspect", "-f", "{{index .Config.Labels \"com.docker.compose.project\"}}", cid).stdout.strip()
        if label:
            projects.add(label)
    if ids:
        docker("rm", "-f", "-v", *sorted(ids))
    for project in sorted(projects):
        volumes = docker("volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}").stdout.split()
        if volumes:
            docker("volume", "rm", "-f", *volumes)
        networks = docker("network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}").stdout.split()
        if networks:
            docker("network", "rm", *networks)


def started_from(cid: str, workdir: Path) -> bool:
    """Compose records the project directory; only containers started under this run's workdir count."""
    label = docker("inspect", "-f", "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}", cid).stdout.strip()
    return bool(label) and (label == str(workdir) or label.startswith(str(workdir) + "/"))


def checkout_context(doc: dict[str, Any], revision: str, session: Session, token: str | None) -> None:
    """A README that lives inside a repository is read from a checkout: clone it, cd where it says.

    The clone is made inside the reader's container, so the reader owns it (git refuses a repository
    another user owns)."""
    spec = doc.get("checkout")
    if not spec:
        return
    target = session.workdir / "app" / doc["repo"].split("/")[-1]
    if not (target / ".git").exists():
        url = f"https://github.com/{doc['repo']}.git"
        script = (f"mkdir -p {shlex.quote(str(target))} && cd {shlex.quote(str(target))} && git init -q && "
                  f"git fetch -q --depth 1 {shlex.quote(url)} {shlex.quote(revision)} && git checkout -q FETCH_HEAD")
        outcome = session._exec(session.container(doc["runtime"]), ["bash", "-c", script], DEFAULT_TIMEOUT)
        if outcome.status != "pass":
            raise RunError(f"could not check out {doc['repo']}@{revision[:12]}: {outcome.stderr.strip()[-300:]}")
    session.cwd = str(target / spec.get("cwd", ""))


def summarize(result: dict[str, Any]) -> None:
    checks = [c for c in result.get("checks", []) if c["check"] != "nothing-executed"]
    if not any("durationSec" in b for b in result["blocks"]):
        checks.append({"check": "nothing-executed", "status": "fail", "detail": "zero blocks executed"})
    result["checks"] = checks
    statuses = [r["status"] for r in result["blocks"]] + [c["status"] for c in result.get("checks", [])]
    result["status"] = ("fail" if any(s in {"fail", "not-evaluated"} for s in statuses) else
                        "needs-input" if "needs-input" in statuses else "blocked" if "blocked" in statuses else "pass")
    result["counts"] = {s: statuses.count(s) for s in sorted(set(statuses))}


def run_teardowns(session: Session, secrets: list[str]) -> None:
    """Run what each document said to run when the reader is done, after the whole session."""
    for row, code, runtime, result in session.teardowns:
        try:
            outcome = session.run_shell(code, runtime, DEFAULT_TIMEOUT, False)
        except Exception:
            outcome = exception_outcome()
        row.update({"status": outcome.status, "detail": "run at the end of the session: " + outcome.detail,
                    "durationSec": round(outcome.duration, 1), "exitCode": outcome.exit_code})
        if outcome.status != "pass":
            row["stdoutTail"] = scrub(tail(outcome.stdout), secrets)
            row["stderrTail"] = scrub(tail(outcome.stderr), secrets)
        summarize(result)
    session.teardowns.clear()


def run_document(doc: dict[str, Any], text: str, session: Session, context: dict[str, str],
                 variables: dict[str, Any], candidate_digest: str, secrets: list[str],
                 defined: set[str], revision: str = "", token: str | None = None,
                 report_row: dict[str, Any] | None = None) -> tuple[dict[str, Any], set[str]]:
    blocks = extract(text, "html" if doc.get("format") == "html" else "markdown")
    context = {**context, "session.appDir": str(session.workdir / "app")}
    if (doc["runtime"] == "dotnet" and not doc.get("checkout")
            and not any(re.search(r"\bdotnet\s+new\b", b.code) for b in blocks)):
        session.ensure_dotnet_project()
    env_values = {k: render(str(v["value"]), context) for k, v in variables["env"].items()}
    subst = {k: render(str(v["value"]), context) for k, v in variables["substitute"].items()}
    unresolved = sorted({f"{section}.{k} -> {v}" for section, table in (("env", env_values), ("substitute", subst))
                         for k, v in table.items() if UNRESOLVED.search(v)})
    if unresolved:
        # Fail closed: a block must never run with a literal `{candidate.image}` (or fall back to the
        # document's own text, e.g. a floating release-channel tag) because the run has no value for it.
        raise RunError("variables reference values this run does not have: " + "; ".join(unresolved))
    session.env.update(env_values)
    packages = [(m.group(1), m.group(2)) for b in blocks if b.language == "shell"
                for m in DOTNET_ADD.finditer(b.code)]
    rows: list[dict[str, Any]] = []
    if report_row is not None:
        report_row["blocks"] = rows
    deferred: list[Block] = []
    pinned_versions = {k.lower() if not k.startswith("@") else k: v for k, v in (context.get("_pins") or {}).items()}
    before = snapshot_containers() if doc.get("docker") else set()
    servers_seen = {cid: meta for cid, meta in getattr(session, "servers_seen", {}).items() if cid in before}
    later_text = {b.index: "\n".join(x.code for x in blocks[b.index + 1:]) for b in blocks}

    def record(block: Block, outcome: Outcome | None, status: str, detail: str, extra: dict | None = None) -> None:
        row = {"index": block.index, "line": block.line, "language": block.language, "intent": block.intent,
               "status": status, "detail": scrub(detail, secrets), "sha256": block.sha256,
               "command": scrub(tail(block.code, 600), secrets)}
        if block.reason:
            row["reason"] = block.reason
        if block.marker_error:
            row["markerError"] = block.marker_error
        if block.blocked_by:
            # The document says this command is right and links the issue that tracks the misbehaviour:
            # a failure is recorded against that issue (the gate stays blocked); a pass means the marker is stale.
            row["blockedBy"] = block.blocked_by
            if status == "fail" and outcome is not None and outcome.command_failed:
                row["status"] = "blocked"
                row["detail"] = scrub(f"blocked by {block.blocked_by}: {detail}", secrets)
            elif status == "pass":
                row["staleBlockedMarker"] = True
                row["detail"] = scrub(f"{detail}; passes, so the doc-run: blocked marker ({block.blocked_by}) "
                                      "no longer applies: remove it", secrets)
        if outcome is not None:
            row["durationSec"] = round(outcome.duration, 1)
            row["exitCode"] = outcome.exit_code
            if outcome.mode:
                row["mode"] = outcome.mode
            if status != "pass":
                row["stdoutTail"] = scrub(tail(outcome.stdout), secrets)
                row["stderrTail"] = scrub(tail(outcome.stderr), secrets)
        row.update(extra or {})
        rows.append(row)

    def audit(outcome: Outcome, runtime: str) -> Outcome:
        installed = session.installed_honua(runtime)
        allowed = {**context.get("_closure", lambda: {})(), **pinned_versions}
        # The registry guard already admits Honua.Sdk.* at the root SDK's exact release version.
        for name in installed:
            if name.startswith("honua.sdk.") and "honua.sdk" in allowed:
                allowed.setdefault(name, allowed["honua.sdk"])
        wrong = sorted(f"{name} {version} (admitted version: {allowed.get(name, 'none')})"
                       for name, version in installed.items() if allowed.get(name) not in {version, "*"})
        if wrong:
            outcome.status = "fail"
            outcome.command_failed = False
            outcome.detail += "; installed a Honua package the release does not pin: " + "; ".join(wrong)
        return outcome

    def dispatch(block: Block, code: str) -> Outcome:
        lang = block.language
        timeout = int(doc.get("timeout", DEFAULT_TIMEOUT))
        if lang == "shell":
            serve = bool(SERVE.search(code))
            readiness = {k: substitute(v, subst) for k, v in (block.marker or {}).items()
                         if k in {"ready-url", "ready-log"}}
            if serve and readiness:
                return session.run_shell(code, doc["runtime"], timeout, True,
                                         {k.removeprefix("ready-"): v for k, v in readiness.items()})
            return session.run_shell(code, doc["runtime"], timeout, serve)
        if lang == "python":
            return session.run_python(code, timeout)
        if lang in {"javascript", "typescript"}:
            return session.run_js(code, lang, timeout)
        if lang == "csharp":
            return session.run_csharp(code, packages, timeout)
        if lang == "http":
            return session.run_http(code, doc["runtime"], context["candidate.baseUrl"], timeout)
        return Outcome("fail", f"no executor for {lang}")

    def execute(block: Block, code: str) -> Outcome:
        return dispatch(block, code)

    pending_teardowns: list[tuple[Block, str]] = []
    for block in blocks:
        try:
            if ((block.marker_error or "").startswith("doc-run: run on a language") and block.intent == "run"
                    or (block.marker_error or "").startswith("doc-run: blocked on a language")):
                record(block, None, "fail", block.marker_error)
                continue
            if block.intent in {"illustrative", "alternative", "excluded", "output"}:
                record(block, None, "not-run", block.reason or f"{block.intent} block")
                continue
            if block.intent == "teardown":
                pending_teardowns.append((block, substitute(block.code, subst)))
                continue
            if block.intent == "compile":
                deferred.append(block)
                continue
            need = needs(block, defined | set(session.env))
            missing_env = [n for n in need["env"] if n not in session.env]
            missing_ph = [p for p in need["placeholders"] if p not in subst]
            defined |= assigned_names(block)
            if missing_env or missing_ph:
                parts = []
                if missing_env:
                    parts.append("environment " + ", ".join(missing_env))
                if missing_ph:
                    parts.append("placeholder " + ", ".join(missing_ph))
                record(block, None, "needs-input",
                       "needs " + "; ".join(parts) + " and the document gives no value a reader could supply "
                       "(no variables-file entry citing where the doc documents it)")
                continue
            code = substitute(block.code, subst)
            if block.marker and "checkout" in block.marker:
                checkout_context({**doc, "checkout": {"cwd": block.marker.get("checkout", "")}}, revision, session, token)
            if block.intent == "file":
                target = Path(session.cwd) / block.file if not block.file.startswith("/") else Path(block.file)
                session.put(doc["runtime"], target, code)
                # A .cs file belongs to the reader's project: a later `dotnet run` builds it, it never runs alone.
                runnable = block.language in {"python", "javascript", "typescript", "shell"}
                if not runnable or Path(block.file).name in later_text[block.index]:
                    record(block, None, "pass", f"saved as {block.file} for the steps that use it", {"file": block.file})
                    continue
                outcome = audit(execute(block, code), LANGUAGE_RUNTIME.get(block.language, doc["runtime"]))
                record(block, outcome, outcome.status, f"saved as {block.file}; no later step runs it, so run it: "
                       + outcome.detail, {"file": block.file})
                continue
            outcome = execute(block, code)
            lang = block.language
            if block.expect_failure and outcome.command_failed and outcome.exit_code not in (None, 0, 124):
                outcome.status, outcome.detail = "pass", f"exit code {outcome.exit_code}, as the document says it should fail"
            elif block.expect_failure and outcome.status == "pass":
                outcome.status, outcome.detail = "fail", "exit code 0, but the document says this command fails"
            if (outcome.status == "fail" and lang in {"csharp", "javascript", "typescript"}
                    and session.passed.get(lang) and continuation_error(lang, outcome.stdout + outcome.stderr)):
                combined = (combine_csharp if lang == "csharp" else combine_js)(session.passed[lang] + [code])
                retry = execute(block, combined)
                retry.mode = (retry.mode or "") + "+continues-earlier-blocks"
                outcome = retry
            if outcome.status == "pass":
                if lang in {"csharp", "javascript", "typescript"}:
                    session.passed.setdefault(lang, []).append(code)
                if block.expected_output:
                    ok, why = assert_output(block.expected_output, outcome.stdout)
                    if not ok:
                        outcome.status, outcome.detail = "fail", f"{outcome.detail}; output assertion failed: {why}"
                        outcome.command_failed = False
                    else:
                        outcome.detail = f"{outcome.detail}; {why}"
            if doc.get("docker") and block.language == "shell":
                observe_servers({c for c in snapshot_containers() - before if started_from(c, session.workdir)},
                                servers_seen, candidate_digest)
            outcome = audit(outcome, LANGUAGE_RUNTIME.get(block.language, doc["runtime"]))
            record(block, outcome, outcome.status, outcome.detail)
        except Exception:
            outcome = exception_outcome()
            record(block, outcome, outcome.status, outcome.detail)
    for block in deferred:
        try:
            outcome = session.run_compile(substitute(block.code, subst), int(doc.get("timeout", DEFAULT_TIMEOUT)))
            outcome = audit(outcome, "node")
        except Exception:
            outcome = exception_outcome()
        record(block, outcome, outcome.status, "doc-test=compile; typechecked after the document's install steps: "
               + outcome.detail)
    for block, code in pending_teardowns:
        record(block, None, "pending", "teardown; runs when the session's documents are done")
        session.teardowns.append((rows[-1], code, doc["runtime"], report_row if report_row is not None else {}))
    rows.sort(key=lambda r: r["index"])
    checks = []
    if doc.get("docker"):
        check = server_container_check(servers_seen, candidate_digest)
        active = snapshot_containers()
        session.servers_seen = {cid: meta for cid, meta in servers_seen.items() if cid in active}
        checks.append(check or {"check": "boots-candidate-image", "status": "not-evaluated",
                                "detail": f"no honua-server container was started from this document's directory "
                                          f"({len(servers_seen)} container(s) started there)"})
    result = {"blocks": rows, "checks": checks}
    summarize(result)
    return (result, defined)


# ── orchestration ────────────────────────────────────────────────────────────────────────────────

def prepare_tools(runtimes: dict[str, str], work: Path) -> Path:
    tools = work / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for runtime in ("node", "python", "dotnet"):
        docker("pull", runtimes[runtime], timeout=1800, check=True)
    node = tools / "node"
    if not (node / "bin" / "node").exists():
        cid = docker("create", runtimes["node"], check=True).stdout.strip()
        try:
            (node / "bin").mkdir(parents=True, exist_ok=True)
            (node / "lib").mkdir(parents=True, exist_ok=True)
            docker("cp", f"{cid}:/usr/local/bin/node", str(node / "bin" / "node"), check=True)
            docker("cp", f"{cid}:/usr/local/lib/node_modules", str(node / "lib"), check=True)
        finally:
            docker("rm", "-f", cid)
        for command, target in (("npm", "npm-cli.js"), ("npx", "npx-cli.js")):
            (node / "bin" / command).symlink_to(f"../lib/node_modules/npm/bin/{target}")
    docker("pull", runtimes["dockerCli"], timeout=900, check=True)
    cid = docker("create", runtimes["dockerCli"], check=True).stdout.strip()
    try:
        docker("cp", f"{cid}:/usr/local/bin/docker", str(tools / "docker"), check=True)
        docker("cp", f"{cid}:/usr/local/libexec/docker/cli-plugins/docker-compose", str(tools / "docker-compose"),
               check=True)
    finally:
        docker("rm", "-f", cid)
    return tools


def candidate_from_manifest(manifest: dict[str, Any]) -> tuple[str, str]:
    server = manifest.get("components", {}).get("honua-server", {})
    image, digest = str(server.get("image", "")), str(server.get("digest", ""))
    if not image or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RunError("the manifest has no honua-server image with an immutable sha256 digest")
    return f"{image.split('@')[0]}@{digest}", digest


def markdown_summary(report: dict[str, Any]) -> str:
    lines = [f"## Executable docs — {report['status']}", "",
             f"Candidate `{report['candidate']['image']}` · {report['summary']}", "",
             "| document | revision | status | pass | fail | needs-input | blocked | not-run |",
             "|---|---|---|---|---|---|---|---|"]
    for doc in report["documents"]:
        c = doc.get("counts", {})
        lines.append(f"| [{doc['repo'].split('/')[-1]}:{doc['path']}]({doc['url']}) | `{doc['revision'][:8]}` | "
                     f"**{doc['status']}** | {c.get('pass', 0)} | {c.get('fail', 0)} | {c.get('needs-input', 0)} | "
                     f"{c.get('blocked', 0)} | {c.get('not-run', 0)} |")
    failing = [(d, b) for d in report["documents"] for b in d.get("blocks", []) if b["status"] in {"fail", "needs-input", "blocked"}]
    if failing:
        lines += ["", "### Blocks requiring attention", ""]
        for doc, block in failing:
            lines.append(f"- `{doc['repo'].split('/')[-1]}:{doc['path']}` block {block['index']} (line {block['line']}, "
                         f"{block['language']}): **{block['status']}** — {block['detail'][:300]}")
    for doc in report["documents"]:
        for check in doc.get("checks", []):
            if check["status"] != "pass":
                lines.append(f"- `{doc['repo'].split('/')[-1]}:{doc['path']}` {check['check']}: **{check['status']}** — "
                             f"{check['detail'][:300]}")
    if report.get("guardRefusals"):
        lines += ["", "### Package installs refused (not a manifest pin)", ""]
        lines += [f"- {r['ecosystem']}: `{r['package']}`" for r in report["guardRefusals"]]
    if report.get("inventoryDrift"):
        lines += ["", "### Inventory drift (regenerate with `inventory.py --write`)", ""]
        lines += [f"- {line}" for line in report["inventoryDrift"]]
    return "\n".join(lines) + "\n"


class Guards:
    """Registry-guard sidecars, one per network the doc containers use, sharing that network."""

    def __init__(self, manifest: dict[str, Any], image: str, work: Path, run_id: str):
        self.dir = work / "guard"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "pins.json").write_text(json.dumps(pins_from_manifest(manifest)))
        shutil.copy2(HERE / "registry_guard.py", self.dir / "registry_guard.py")
        self.image, self.run_id = image, run_id
        self.port = int(os.environ.get("EXECDOCS_GUARD_PORT", "18765"))
        self.running: dict[str, str] = {}

    def base(self, network: str) -> str:
        if network not in self.running:
            name = f"execdocs-{self.run_id}-guard-{len(self.running)}"
            refusals = self.dir / f"refusals-{len(self.running)}.json"
            docker("rm", "-f", name)
            docker("run", "-d", "--name", name, "--network", network,
                   "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{self.dir}:{self.dir}",
                   "--label", f"honua.execdocs.run={self.run_id}", self.image, "python3", "-u",
                   str(self.dir / "registry_guard.py"), "--pins", str(self.dir / "pins.json"),
                   "--port", str(self.port), "--refusals", str(refusals), check=True)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if "registry guard on" in docker("logs", name).stdout:
                    break
                time.sleep(1)
            else:
                raise RunError(f"registry guard did not start on {network}: {docker('logs', name).stderr[-500:]}")
            self.running[network] = name
        return f"http://127.0.0.1:{self.port}"

    def closure(self) -> dict[str, str]:
        admitted: dict[str, str] = {}
        for path in sorted(self.dir.glob("closure-*.json")):
            admitted.update(json.loads(path.read_text() or "{}"))
        return dict(sorted(admitted.items()))

    def refusals(self) -> list[dict[str, str]]:
        seen: list[dict[str, str]] = []
        for path in sorted(self.dir.glob("refusals-*.json")):
            for entry in json.loads(path.read_text() or "[]"):
                if entry not in seen:
                    seen.append(entry)
        return seen

    def stop(self) -> None:
        for name in self.running.values():
            docker("rm", "-f", name)
        self.running.clear()


def compose_env() -> dict[str, str]:
    return os.environ.copy()


def candidate_container() -> str:
    proc = subprocess.run(["docker", "compose", "-f", str(ROOT / "e2e/harness/compose.candidate.yml"), "ps", "-q",
                           "server"], capture_output=True, text=True, env=compose_env(), check=False)
    cid = proc.stdout.strip().splitlines()
    if not cid:
        raise RunError("the booted candidate server container was not found")
    return cid[0]


def refresh_seed_bindings(context: dict[str, Any]) -> None:
    """Bind fixture layer ids from the manifest the seed wrote, when that file exists.

    ``fixture.mauiBuildingsLayerId`` is the id the admin API returned for the
    synthetic buildings service. It is not the public demo's layer 13. An older
    manifest that has no buildings entry leaves the key unset.
    """
    manifest_path = Path(os.environ.get("E2E_OUT", str(ROOT / "out"))) / "seed-manifest.json"
    if not manifest_path.is_file():
        return
    import importlib.util
    path = ROOT / "e2e/harness/seed/plan.py"
    spec = importlib.util.spec_from_file_location("honua_release_seed_plan", path)
    if spec is None or spec.loader is None:
        raise RunError(f"could not load the seed plan at {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclass looks the class's module up in sys.modules while the class body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    context.update(module.bind_fixture_context(manifest))


def boot_candidate() -> None:
    """Use the canonical stack with a TCP database probe, then its normal health/licensing oracle."""
    command = ["docker", "compose", "-f", str(ROOT / "e2e/harness/compose.candidate.yml"),
               "-f", str(HERE / "compose.readiness.yml"), "up", "-d"]
    if subprocess.run(command, cwd=ROOT).returncode:
        raise RunError("the candidate stack could not start")
    if subprocess.run(["bash", str(ROOT / "e2e/harness/boot.sh"), "wait"], cwd=ROOT).returncode:
        raise RunError("the candidate server did not become ready (see e2e/out/boot.json)")
    if subprocess.run(["bash", str(ROOT / "e2e/harness/seed/seed.sh")], cwd=ROOT).returncode:
        raise RunError("the candidate fixture could not be seeded")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--sources", type=Path, default=HERE / "sources.json")
    parser.add_argument("--inventory", type=Path, default=HERE / "inventory.json")
    parser.add_argument("--vars-dir", type=Path, default=HERE / "vars")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/executable-docs/report.json")
    parser.add_argument("--summary", type=Path, help="also write the per-document markdown summary here")
    parser.add_argument("--evidence-uri", required=True)
    parser.add_argument("--boot", action="store_true", help="boot and seed the candidate with e2e/harness")
    parser.add_argument("--network", choices=("host", "candidate"), default="host",
                        help="host: doc containers use the host network (CI). candidate: client docs share the "
                             "booted candidate server's network namespace, so localhost:8080 is the candidate "
                             "even when another stack holds the host's 8080 (local runs)")
    parser.add_argument("--only", action="append", default=[], help="document id or session (repeatable)")
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args()

    sources = json.loads(args.sources.read_text(encoding="utf-8"))
    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
    try:
        image, digest = candidate_from_manifest(manifest)
    except RunError as exc:
        print(f"executable-docs input error: {exc}", file=sys.stderr)
        return 2
    if os.environ.get("HONUA_SERVER_IMAGE") and os.environ["HONUA_SERVER_IMAGE"] != image:
        print("executable-docs input error: HONUA_SERVER_IMAGE overrides the manifest candidate", file=sys.stderr)
        return 2
    api_key = os.environ.get("E2E_API_KEY", "honua-console-dev-key")
    base_url = "http://localhost:8080"   # what the docs' readers see: the candidate on its own port
    context: dict[str, Any] = {"candidate.baseUrl": base_url, "candidate.apiKey": api_key,
                               "candidate.adminPassword": api_key, "candidate.image": image,
                               "candidate.mcpUrl": base_url + "/mcp", "candidate.grpcAddress": "localhost:8081",
                               # a published layer of e2e/harness/seed (the seed asserts maui-zoning -> layer 2).
                               # maui-buildings is filled from the seed manifest when the seed has run:
                               # the returned id, not the public demo's layer 13.
                               "fixture.featureService": "maui-zoning", "fixture.featureLayerId": "2"}
    refresh_seed_bindings(context)
    context["_pins"] = {str(v["package"]): str(v["version"]) for v in manifest.get("clientArtifacts", {}).values()
                        if v.get("package") and v.get("version")}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    resolver = Resolver(manifest, token=token)
    run_id = datetime.now(timezone.utc).strftime("%H%M%S")
    work = (args.workdir or Path(tempfile.mkdtemp(prefix="honua-execdocs-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    selected = [d for d in sources["documents"]
                if not args.only or doc_id(d["repo"], d["path"]) in args.only or d.get("session") in args.only]
    # Documents that boot their own stack run first, on the host network, while the host's ports are
    # free; client documents then run against the booted candidate.
    install_docs = [d for d in selected if d.get("docker")]
    client_docs = [d for d in selected if not d.get("docker")]
    guards = Guards(manifest, sources["runtimes"]["python"], work, run_id)
    context["_closure"] = guards.closure
    booted = False
    report_docs: list[dict[str, Any]] = []
    live_docs: list[tuple[dict[str, Any], str, str]] = []
    sessions: dict[str, Session] = {}

    def add_exception(row: dict[str, Any], check: str) -> None:
        outcome = exception_outcome()
        row.setdefault("blocks", [])
        row.setdefault("checks", []).append({"check": check, "status": "fail",
                                            "detail": scrub(outcome.stderr, [api_key])})
        summarize(row)

    def close_session(session: Session) -> None:
        session.close()
        cleanup_containers({c for c in snapshot_containers() if started_from(c, session.workdir)})

    def run_phase(documents: list[dict[str, Any]], network: str, tools: Path) -> None:
        last_of_session = {(d.get("session") or doc_id(d["repo"], d["path"])): doc_id(d["repo"], d["path"])
                           for d in documents}
        defined: dict[str, set[str]] = {}
        for document in documents:
            ident = doc_id(document["repo"], document["path"])
            row: dict[str, Any] = {"id": ident, "repo": document["repo"], "path": document["path"],
                                   "revision": "unresolved", "url": "", "blocks": [], "checks": []}
            report_docs.append(row)
            key = document.get("session") or ident
            text = None
            try:
                revision = resolver.revision(document)
                text = resolver.read(document, revision)
                row["revision"] = revision
                row["url"] = f"https://github.com/{document['repo']}/blob/{revision}/{document['path']}"
                live_docs.append((document, revision, text))
                if key not in sessions:
                    members = [d for d in documents if (d.get("session") or doc_id(d["repo"], d["path"])) == key]
                    sessions[key] = Session(key, work / key, sources["runtimes"], guards.base(network), tools,
                                            bool(document.get("docker")), network, run_id,
                                            sources.get("toolchain", {}).get("typescript", "5.9.3"),
                                            prerequisites={p for d in members for p in d.get("prerequisites", {})})
                session = sessions[key]
                variables = load_vars(args.vars_dir, ident)
                print(f"== {ident} @ {revision[:12]}", flush=True)
                checkout_context(document, revision, session, token)
                result, defined[key] = run_document(document, text, session, context, variables, digest,
                                                    [api_key], defined.get(key, set()), revision, token, row)
                row.update(result)
                row["variablesFile"] = f"vars/{ident}.json" if (args.vars_dir / f"{ident}.json").exists() else None
            except Exception:
                outcome = exception_outcome()
                if text is not None:
                    recorded = {b["index"] for b in row["blocks"]}
                    for block in extract(text, "html" if document.get("format") == "html" else "markdown"):
                        if block.index not in recorded:
                            row["blocks"].append({**block.record(), "status": "fail", "detail": outcome.detail,
                                                  "command": scrub(block.code, [api_key]),
                                                  "stderrTail": scrub(outcome.stderr, [api_key])})
                add_exception(row, "runner")
            if last_of_session.get(key) == ident and key in sessions:
                try:
                    run_teardowns(sessions[key], [api_key])
                    close_session(sessions[key])
                except Exception:
                    add_exception(row, "cleanup")
            print(f"   {row['status']} {row['counts']}", flush=True)

    try:
        tools = prepare_tools(sources["runtimes"], work)
        run_phase(install_docs, "host", tools)
        if client_docs:
            if args.boot:
                os.environ["HONUA_SERVER_IMAGE"] = image
                booted = True   # tear down whatever `up` started, even when it never became ready
                boot_candidate()
                refresh_seed_bindings(context)
            network = "host" if args.network == "host" else f"container:{candidate_container()}"
            run_phase(client_docs, network, tools)
    except Exception:
        exc = scrub(tail(traceback.format_exc()), [api_key])
        report_docs.append({"id": "candidate", "repo": "honua-io/honua-release", "path": "-", "revision": "-",
                            "url": args.evidence_uri, "status": "fail", "counts": {"fail": 1}, "blocks": [],
                            "checks": [{"check": "candidate", "status": "fail", "detail": str(exc)}]})
    finally:
        for session in sessions.values():
            try:
                close_session(session)
            except Exception:
                add_exception(report_docs[-1], "cleanup")
        refusals, closure = [], {}
        try:
            refusals = guards.refusals()
            closure = guards.closure()
        except Exception:
            add_exception(report_docs[-1], "registry-report")
        try:
            guards.stop()
            if booted:
                subprocess.run(["bash", str(ROOT / "e2e/harness/boot.sh"), "down"], cwd=ROOT, check=True)
        except Exception:
            add_exception(report_docs[-1], "cleanup")
    reported = {d["id"] for d in report_docs}
    for document in selected:
        ident = doc_id(document["repo"], document["path"])
        if ident not in reported:
            row = {"id": ident, "repo": document["repo"], "path": document["path"], "revision": "unresolved",
                   "url": "", "blocks": [], "checks": [{"check": "runner", "status": "fail",
                   "detail": "document could not run because candidate/infrastructure setup failed"}]}
            summarize(row)
            report_docs.append(row)
    committed = json.loads(args.inventory.read_text(encoding="utf-8")) if args.inventory.exists() else {}
    drift_lines: list[str] = []
    if not args.only and committed and len(live_docs) == len(selected):
        current_docs = []
        session_env: dict[str, set[str]] = {}
        for document, revision, text in live_docs:
            record, env = document_record(document, revision, text, session_env.get(document.get("session") or ""))
            if document.get("session"):
                session_env[document["session"]] = env
            current_docs.append(record)
        drift_lines = drift(committed, {"documents": current_docs})
    order = {doc_id(d["repo"], d["path"]): i for i, d in enumerate(sources["documents"])}
    report_docs.sort(key=lambda d: order.get(d["id"], len(order)))
    statuses = [d["status"] for d in report_docs]
    status = ("fail" if drift_lines or "fail" in statuses or not report_docs else
              "blocked" if {"needs-input", "blocked"} & set(statuses) else "pass")
    report = {
        "schemaVersion": 1,
        "gate": "executable-docs",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "release": manifest.get("platformRelease"),
        "candidate": {"image": image, "sourceSha": manifest["components"]["honua-server"].get("sha"),
                      "licensing": "disabled (e2e/harness/compose.candidate.yml)"},
        "clientPins": {k: f"{v.get('package')}@{v.get('version')}" for k, v in manifest.get("clientArtifacts", {}).items()},
        "runtimes": sources["runtimes"],
        "evidenceUri": args.evidence_uri,
        "status": status,
        "summary": {s: statuses.count(s) for s in sorted(set(statuses))},
        "documents": report_docs,
        "outOfScope": sources.get("outOfScope", []),
        "guardRefusals": refusals,
        "guardAdmittedClosure": closure,
        "inventoryDrift": drift_lines,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = markdown_summary(report)
    if args.summary:
        args.summary.write_text(summary, encoding="utf-8")
    print(summary)
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
