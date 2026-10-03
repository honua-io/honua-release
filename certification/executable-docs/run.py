#!/usr/bin/env python3
"""Run every getting-started block of the public docs against the booted release candidate.

    python certification/executable-docs/run.py --boot --evidence-uri <run url>

For each document in sources.json (read at its release revision, see inventory.py) the runner
starts clean containers from digest-pinned node / python / dotnet images, points npm, pip and NuGet
at the registry guard (only manifest-pinned Honua packages are installable), and executes the
document's blocks in order against the candidate `image@digest` booted by e2e/harness/boot.sh with
licensing disabled. Result per block: pass, fail (with the stdout/stderr tail) or needs-input. A
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
from registry_guard import pins_from_manifest  # noqa: E402

TAIL = 2000
SERVE = re.compile(
    r"^\s*(?:npm\s+(?:run\s+)?(?:dev|start|serve|preview)|pnpm\s+(?:run\s+)?(?:dev|start)|yarn\s+(?:dev|start)|"
    r"npx\s+(?:-y\s+)?(?:vite|serve|http-server)|vite\b|python3?\s+-m\s+http\.server|uvicorn\b|flask\s+run|"
    r"dotnet\s+watch\b|docker\s+compose\s+up(?!.*\s-d\b)(?!.*--detach))", re.M)
DEFAULT_TIMEOUT = 600
SERVE_WINDOW = 60
SECRETISH = re.compile(
    r"(([Pp]ass(word|wd)?|PASSWORD|[Ss]ecret|SECRET|[Tt]oken|TOKEN|[Aa]pi[-_]?[Kk]ey|API[-_]?KEY|MASTER_KEY)"
    r"[\"']?\s*[=:]\s*[\"']?)[^\s\"';,]+")


class RunError(RuntimeError):
    pass


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return SECRETISH.sub(r"\1***", text)


def tail(text: str, limit: int = TAIL) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


# ── oracle ───────────────────────────────────────────────────────────────────────────────────────

def _norm(line: str) -> str:
    line = re.sub(r"\d+(\.\d+)?", "0", line)
    return re.sub(r"\s+", " ", line).strip()


def assert_output(expected: str, actual: str) -> tuple[bool, str]:
    """Every non-elided expected line appears in the output, in order (digits and spacing normalized).

    Lines that are only `...`/`…` or comments elide. JSON output compares the expected object's keys.
    """
    try:
        want = json.loads(expected)
        got = json.loads(actual.strip().splitlines()[-1] if actual.strip() else "")
        if isinstance(want, dict) and isinstance(got, dict):
            missing = sorted(set(want) - set(got))
            return (not missing, "expected JSON keys present" if not missing else f"output lacks keys {missing}")
    except (json.JSONDecodeError, IndexError):
        pass
    lines = [_norm(l) for l in expected.splitlines()]
    lines = [l for l in lines if l and l not in {"...", "…"} and not l.startswith("#")]
    haystack = _norm(actual)
    position = 0
    for line in lines:
        found = haystack.find(line, position)
        if found < 0:
            return False, f"expected output line not found: {line[:200]!r}"
        position = found + len(line)
    return True, f"{len(lines)} expected output line(s) matched"


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
    counter: int = 0

    def __post_init__(self) -> None:
        self.state = self.workdir / ".docrun"
        self.state.mkdir(parents=True, exist_ok=True)
        (self.workdir / "app").mkdir(exist_ok=True)
        self.cwd = str(self.workdir / "app")
        nuget = self.state / "nuget"
        nuget.mkdir(exist_ok=True)
        (nuget / "NuGet.Config").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<configuration>\n  <packageSources>\n    <clear />\n'
            f'    <add key="release-pinned" value="{self.guard_base}/nuget/v3/index.json" allowInsecureConnections="true" />\n'
            "  </packageSources>\n</configuration>\n")
        shutil.copy2(HERE / "pysession.py", self.state / "pysession.py")

    # container lifecycle
    def container(self, runtime: str) -> str:
        if runtime in self.containers:
            return self.containers[runtime]
        image = self.runtimes[runtime]
        name = f"execdocs-{self.run_id}-{re.sub(r'[^a-z0-9]+', '-', self.name.lower())[:40]}-{runtime}"
        g = self.guard_base
        args = ["run", "-d", "--name", name, "--network", self.network,
                "-v", f"{self.workdir}:{self.workdir}", "-w", self.cwd,
                "-v", f"{self.state / 'nuget' / 'NuGet.Config'}:/root/.nuget/NuGet/NuGet.Config:ro",
                "-e", f"npm_config_registry={g}/npm/", "-e", "npm_config_update_notifier=false",
                "-e", "npm_config_fund=false", "-e", f"YARN_NPM_REGISTRY_SERVER={g}/npm/",
                "-e", f"PIP_INDEX_URL={g}/pypi/simple/", "-e", f"UV_DEFAULT_INDEX={g}/pypi/simple/",
                "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1", "-e", "PIP_ROOT_USER_ACTION=ignore",
                "-e", "DOTNET_CLI_TELEMETRY_OPTOUT=1", "-e", "DOTNET_NOLOGO=1",
                "--label", f"honua.execdocs.run={self.run_id}"]
        if "node" in self.prerequisites and runtime != "node":
            args += ["-v", f"{self.tools / 'node'}:/opt/node:ro",
                     "-e", "PATH=/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
        if self.docker_access:
            args += ["-v", "/var/run/docker.sock:/var/run/docker.sock",
                     "-v", f"{self.tools / 'docker'}:/usr/local/bin/docker:ro",
                     "-v", f"{self.tools / 'docker-compose'}:/usr/local/lib/docker/cli-plugins/docker-compose:ro"]
        args += [image, "sleep", "infinity"]
        docker("rm", "-f", name)
        docker(*args, check=True, timeout=1800)
        self.containers[runtime] = name
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

    def put(self, runtime: str, path: Path, content: str) -> None:
        """Write into the reader's directories from inside the container: the reader owns them there
        (on a Linux host, files a root container created are not writable by the runner's user)."""
        proc = subprocess.run(["docker", "exec", "-i", self.container(runtime), "sh", "-c",
                               'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", str(path)],
                              input=content, text=True, capture_output=True, check=False)
        if proc.returncode:
            raise RunError(f"could not write {path}: {proc.stderr.strip()[-300:]}")

    # executors
    def run_shell(self, code: str, runtime: str, timeout: int, serve: bool) -> Outcome:
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
        proc = subprocess.run(
            ["docker", "exec", *self.exec_env(runtime), name, "timeout", "-k", "5", str(window), "bash", "-c",
             f"{wrapper}", "docrun"],
            stdin=subprocess.DEVNULL, stdout=open(out, "w"), stderr=open(err, "w"), timeout=window + 60, check=False)
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
                return Outcome("pass", f"long-running command kept serving for {window}s (stopped by the runner)",
                               stdout, stderr, code_, duration, "serve")
            return Outcome("fail", f"timed out after {window}s (waiting for input or a process that never ends)",
                           stdout, stderr, code_, duration)
        if code_:
            return Outcome("fail", f"exit code {code_}", stdout, stderr, code_, duration)
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
        return Outcome("fail", last[-1][:300] if last else "exception", stdout, stderr, 1, duration)

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
            out = docker("exec", *self.exec_env(runtime), name, "python3", "-m", "pip", "list", "--format",
                         "json").stdout
            try:
                for row in json.loads(out or "[]"):
                    if row["name"].lower().startswith("honua"):
                        found[row["name"].lower().replace("_", "-")] = row["version"]
            except (json.JSONDecodeError, KeyError):
                pass
        modules = Path(self.cwd) / "node_modules"
        for package_json in list(modules.glob("@honua/*/package.json")) + list(modules.glob("@honua-io/*/package.json")):
            try:
                meta = json.loads(package_json.read_text())
                found[meta["name"]] = meta["version"]
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        return found

    def _exec(self, name: str, cmd: list[str], timeout: int) -> Outcome:
        started = time.monotonic()
        out, err = self.next_path(".out"), self.next_path(".err")
        runtime = next((key for key, value in self.containers.items() if value == name), None)
        proc = subprocess.run(["docker", "exec", "-w", self.cwd, *self.exec_env(runtime), name,
                               "timeout", "-k", "5", str(timeout), *cmd],
                              stdin=subprocess.DEVNULL, stdout=open(out, "w"), stderr=open(err, "w"),
                              timeout=timeout + 60, check=False)
        stdout, stderr = out.read_text(errors="replace"), err.read_text(errors="replace")
        duration = time.monotonic() - started
        if proc.returncode == 124:
            return Outcome("fail", f"timed out after {timeout}s", stdout, stderr, 124, duration)
        if proc.returncode:
            return Outcome("fail", f"exit code {proc.returncode}", stdout, stderr, proc.returncode, duration)
        return Outcome("pass", "exit code 0", stdout, stderr, 0, duration)


def _read(path: Path, suffix: str) -> str:
    target = Path(str(path) + suffix)
    return target.read_text(errors="replace") if target.exists() else ""


DOTNET_ADD = re.compile(r"dotnet\s+add\s+(?:\S+\s+)?package\s+([\w.]+)(?:\s+(?:--version|-v)\s+([\w.\-+*]+))?")


def substitute(code: str, table: dict[str, str]) -> str:
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
    """A README that lives inside a repository is read from a checkout: clone it, cd where it says."""
    spec = doc.get("checkout")
    if not spec:
        return
    target = session.workdir / "app" / doc["repo"].split("/")[-1]
    if not (target / ".git").exists():
        target.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{doc['repo']}.git"
        if token:
            url = f"https://x-access-token:{token}@github.com/{doc['repo']}.git"
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        for cmd in (["git", "init", "-q"], ["git", "fetch", "-q", "--depth", "1", url, revision],
                    ["git", "checkout", "-q", "FETCH_HEAD"]):
            proc = subprocess.run(cmd, cwd=target, capture_output=True, text=True, env=env, check=False)
            if proc.returncode:
                raise RunError(f"could not check out {doc['repo']}@{revision[:12]}: {proc.stderr.strip()[-300:]}")
    session.cwd = str(target / spec.get("cwd", ""))


def summarize(result: dict[str, Any]) -> None:
    statuses = [r["status"] for r in result["blocks"]] + [c["status"] for c in result.get("checks", [])]
    result["status"] = "fail" if "fail" in statuses else ("needs-input" if "needs-input" in statuses else "pass")
    result["counts"] = {s: statuses.count(s) for s in sorted(set(statuses))}


def run_teardowns(session: Session, secrets: list[str]) -> None:
    """Run what each document said to run when the reader is done, after the whole session."""
    for row, code, runtime, result in session.teardowns:
        outcome = session.run_shell(code, runtime, DEFAULT_TIMEOUT, False)
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
    env_values = {k: render(str(v["value"]), context) for k, v in variables["env"].items()}
    subst = {k: render(str(v["value"]), context) for k, v in variables["substitute"].items()}
    session.env.update(env_values)
    packages = [(m.group(1), m.group(2)) for b in blocks if b.language == "shell"
                for m in DOTNET_ADD.finditer(b.code)]
    rows: list[dict[str, Any]] = []
    deferred: list[Block] = []
    pinned_versions = {k.lower() if not k.startswith("@") else k: v for k, v in (context.get("_pins") or {}).items()}
    before = snapshot_containers() if doc.get("docker") else set()
    servers_seen: dict[str, dict[str, Any]] = {}
    later_text = {b.index: "\n".join(x.code for x in blocks[b.index + 1:]) for b in blocks}

    def record(block: Block, outcome: Outcome | None, status: str, detail: str, extra: dict | None = None) -> None:
        row = {"index": block.index, "line": block.line, "language": block.language, "intent": block.intent,
               "status": status, "detail": scrub(detail, secrets), "sha256": block.sha256,
               "command": scrub(tail(block.code, 600), secrets)}
        if block.reason:
            row["reason"] = block.reason
        if block.marker_error:
            row["markerError"] = block.marker_error
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

    def execute(block: Block, code: str) -> Outcome:
        lang = block.language
        timeout = int(doc.get("timeout", DEFAULT_TIMEOUT))
        if lang == "shell":
            return session.run_shell(code, doc["runtime"], timeout, bool(SERVE.search(code)))
        if lang == "python":
            return session.run_python(code, timeout)
        if lang in {"javascript", "typescript"}:
            return session.run_js(code, lang, timeout)
        if lang == "csharp":
            return session.run_csharp(code, packages, timeout)
        if lang == "http":
            return session.run_http(code, doc["runtime"], context["candidate.baseUrl"], timeout)
        return Outcome("fail", f"no executor for {lang}")

    pending_teardowns: list[tuple[Block, str]] = []
    for block in blocks:
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
            try:
                checkout_context({**doc, "checkout": {"cwd": block.marker.get("checkout", "")}}, revision, session, token)
            except RunError as exc:
                record(block, None, "fail", str(exc))
                continue
        if block.intent == "file":
            target = Path(session.cwd) / block.file if not block.file.startswith("/") else Path(block.file)
            session.put(doc["runtime"], target, code)
            runnable = block.language in {"python", "javascript", "typescript", "shell", "csharp"}
            if not runnable or Path(block.file).name in later_text[block.index]:
                record(block, None, "pass", f"saved as {block.file} for the steps that use it", {"file": block.file})
                continue
            outcome = execute(block, code)
            record(block, outcome, outcome.status, f"saved as {block.file}; no later step runs it, so run it: "
                   + outcome.detail, {"file": block.file})
            continue
        outcome = execute(block, code)
        lang = block.language
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
                else:
                    outcome.detail = f"{outcome.detail}; {why}"
        if doc.get("docker") and block.language == "shell":
            observe_servers({c for c in snapshot_containers() - before if started_from(c, session.workdir)},
                            servers_seen, candidate_digest)
        if outcome.status == "pass" and block.language == "shell" and pinned_versions:
            installed = session.installed_honua(doc["runtime"])
            wrong = sorted(f"{name} {version} (the release pins {pinned_versions[name]})"
                           for name, version in installed.items()
                           if name in pinned_versions and version != pinned_versions[name])
            if wrong:
                outcome.status = "fail"
                outcome.detail = ("installed a Honua package the release does not pin: " + "; ".join(wrong))
        record(block, outcome, outcome.status, outcome.detail)
    for block in deferred:
        outcome = session.run_compile(substitute(block.code, subst), int(doc.get("timeout", DEFAULT_TIMEOUT)))
        record(block, outcome, outcome.status, "doc-test=compile; typechecked after the document's install steps: "
               + outcome.detail)
    for block, code in pending_teardowns:
        record(block, None, "pending", "teardown; runs when the session's documents are done")
        session.teardowns.append((rows[-1], code, doc["runtime"], report_row if report_row is not None else {}))
    rows.sort(key=lambda r: r["index"])
    checks = []
    if doc.get("docker"):
        check = server_container_check(servers_seen, candidate_digest)
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
             "| document | revision | status | pass | fail | needs-input | not-run |", "|---|---|---|---|---|---|---|"]
    for doc in report["documents"]:
        c = doc.get("counts", {})
        lines.append(f"| [{doc['repo'].split('/')[-1]}:{doc['path']}]({doc['url']}) | `{doc['revision'][:8]}` | "
                     f"**{doc['status']}** | {c.get('pass', 0)} | {c.get('fail', 0)} | {c.get('needs-input', 0)} | "
                     f"{c.get('not-run', 0)} |")
    failing = [(d, b) for d in report["documents"] for b in d.get("blocks", []) if b["status"] in {"fail", "needs-input"}]
    if failing:
        lines += ["", "### Blocks that do not run", ""]
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
            docker("run", "-d", "--name", name, "--network", network, "-v", f"{self.dir}:{self.dir}",
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
                               "candidate.mcpUrl": base_url + "/mcp", "candidate.grpcAddress": "localhost:8081"}
    context["_pins"] = {str(v["package"]): str(v["version"]) for v in manifest.get("clientArtifacts", {}).values()
                        if v.get("package") and v.get("version") and not str(v["package"]).startswith("Honua.")}
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
    booted = False
    report_docs: list[dict[str, Any]] = []
    live_docs: list[tuple[dict[str, Any], str, str]] = []
    sessions: dict[str, Session] = {}

    def run_phase(documents: list[dict[str, Any]], network: str, tools: Path) -> None:
        last_of_session = {(d.get("session") or doc_id(d["repo"], d["path"])): doc_id(d["repo"], d["path"])
                           for d in documents}
        defined: dict[str, set[str]] = {}
        for document in documents:
            ident = doc_id(document["repo"], document["path"])
            row: dict[str, Any] = {"id": ident, "repo": document["repo"], "path": document["path"]}
            try:
                revision = resolver.revision(document)
                text = resolver.read(document, revision)
            except InventoryError as exc:
                row.update({"revision": "unresolved", "url": "", "status": "fail", "counts": {"fail": 1},
                            "blocks": [], "checks": [{"check": "read-document", "status": "fail", "detail": str(exc)}]})
                report_docs.append(row)
                continue
            row["revision"] = revision
            row["url"] = f"https://github.com/{document['repo']}/blob/{revision}/{document['path']}"
            key = document.get("session") or ident
            if key not in sessions:
                members = [d for d in documents if (d.get("session") or doc_id(d["repo"], d["path"])) == key]
                sessions[key] = Session(key, work / key, sources["runtimes"], guards.base(network), tools,
                                        bool(document.get("docker")), network, run_id,
                                        sources.get("toolchain", {}).get("typescript", "5.9.3"),
                                        prerequisites={p for d in members for p in d.get("prerequisites", {})})
            session = sessions[key]
            variables = load_vars(args.vars_dir, ident)
            print(f"== {ident} @ {revision[:12]}", flush=True)
            try:
                checkout_context(document, revision, session, token)
            except RunError as exc:
                row.update({"status": "fail", "counts": {"fail": 1}, "blocks": [],
                            "checks": [{"check": "checkout", "status": "fail", "detail": str(exc)}]})
                report_docs.append(row)
                continue
            try:
                result, defined[key] = run_document(document, text, session, context, variables, digest,
                                                    [api_key], defined.get(key, set()), revision, token, row)
            except (RunError, OSError, subprocess.SubprocessError, ValueError) as exc:
                result = {"status": "fail", "counts": {"fail": 1}, "blocks": [],
                          "checks": [{"check": "runner", "status": "fail",
                                      "detail": f"the runner could not execute this document: {exc}"}]}
            row.update(result)
            row["variablesFile"] = f"vars/{ident}.json" if (args.vars_dir / f"{ident}.json").exists() else None
            report_docs.append(row)
            live_docs.append((document, revision, text))
            print(f"   {row['status']} {row['counts']}", flush=True)
            if last_of_session.get(key) == ident:
                run_teardowns(session, [api_key])
                session.close()
                cleanup_containers({c for c in snapshot_containers() if started_from(c, session.workdir)})

    try:
        tools = prepare_tools(sources["runtimes"], work)
        run_phase(install_docs, "host", tools)
        if client_docs:
            if args.boot:
                os.environ["HONUA_SERVER_IMAGE"] = image
                booted = True   # tear down whatever `up` started, even when it never became ready
                if subprocess.run(["bash", str(ROOT / "e2e/harness/boot.sh"), "up"], cwd=ROOT).returncode:
                    raise RunError("the candidate server did not become ready (see e2e/out/boot.json)")
                if subprocess.run(["bash", str(ROOT / "e2e/harness/seed/seed.sh")], cwd=ROOT).returncode:
                    raise RunError("the candidate fixture could not be seeded")
            network = "host" if args.network == "host" else f"container:{candidate_container()}"
            run_phase(client_docs, network, tools)
    except RunError as exc:
        report_docs.append({"id": "candidate", "repo": "honua-io/honua-release", "path": "-", "revision": "-",
                            "url": args.evidence_uri, "status": "fail", "counts": {"fail": 1}, "blocks": [],
                            "checks": [{"check": "candidate", "status": "fail", "detail": str(exc)}]})
    finally:
        for session in sessions.values():
            session.close()
            cleanup_containers({c for c in snapshot_containers() if started_from(c, session.workdir)})
        refusals = guards.refusals()
        closure = guards.closure()
        guards.stop()
        if booted:
            subprocess.run(["bash", str(ROOT / "e2e/harness/boot.sh"), "down"], cwd=ROOT)
    committed = json.loads(args.inventory.read_text(encoding="utf-8")) if args.inventory.exists() else {}
    drift_lines: list[str] = []
    if not args.only and committed:
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
    status = "fail" if "fail" in statuses or not report_docs else ("blocked" if "needs-input" in statuses else "pass")
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
