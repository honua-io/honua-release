#!/usr/bin/env python3
"""Run the public Compose quickstart and write a G3 receipt.

The page is honua-server ``docs/get-started/quickstart.md``. Annotated fences
run in document order. ``mode=skip`` fences are not executed. Yaml and Python
fences are written to the filenames the page names. Bash fences are copied
unchanged into one shell so a virtualenv activation persists. The only
permitted edit is the ``HONUA_IMAGE`` assignment, and only when the caller
names a different image.

``.github/workflows/g3-compose-quickstart.yml`` is the dispatch entry point.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MARK = "::__G3_STEP__"
ANNOTATION = re.compile(r"^<!-- docs-validation:(?P<id>[a-z0-9.-]+) (?P<attrs>.+) -->$")
FENCE_OPEN = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$")
FENCE_CLOSE = re.compile(r"^```\s*$")
SAVE_AS = re.compile(r"as `([^`]+)`")
IMAGE_LINE = re.compile(r"^HONUA_IMAGE=(\S+)$", re.MULTILINE)
FILENAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
BARE_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
FULL_TAG = re.compile(
    r"ghcr\.io/honua-io/honua-server:(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})\Z"
)
FULL_DIGEST = re.compile(r"ghcr\.io/honua-io/honua-server@sha256:[0-9a-f]{64}\Z")
COMPOSE_VERSION = re.compile(r"v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
RUN_NUMBER = re.compile(r"\d+\Z")
STARTED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
SECRET_KEYS = (
    "POSTGRES_PASSWORD",
    "HONUA_ADMIN_PASSWORD",
    "HONUA_MASTER_KEY",
    "HONUA_KEYRING_PASSWORD",
)
QUERY_WORDS = ("west", "east")
MIN_COMPOSE = (2, 23, 1)
SHELL_TIMEOUT = 35 * 60
TEARDOWN_TIMEOUT = 180

Runner = Callable[[list[str], Path, int], tuple[int, str]]


class QuickstartError(ValueError):
    """The page or the caller input cannot be executed as a G3 run."""


@dataclass
class Fence:
    block_id: str
    mode: str
    language: str
    reason: str
    body: str
    line: int


@dataclass
class Action:
    block_id: str
    kind: str
    language: str
    body: str
    filename: str
    reason: str


@dataclass
class Step:
    block_id: str
    kind: str
    status: str
    started: str
    ended: str
    exit_code: int | None
    detail: str
    output: str
    reason: str = ""


@dataclass
class TraceEvent:
    started: str = ""
    ended: str = ""
    output: list[str] = field(default_factory=list)
    filename: str = ""
    complete: bool = False


@dataclass
class Execution:
    steps: list[Step]
    shell_exit: int | None
    teardown: str


def parse_document(text: str) -> list[Fence]:
    """Return annotated fences in order. An unannotated fence is an error."""
    lines = text.splitlines()
    fences: list[Fence] = []
    seen: set[str] = set()
    pending: tuple[str, dict[str, str], int] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        annotation = ANNOTATION.match(line)
        if annotation:
            if pending is not None:
                raise QuickstartError(
                    f"line {index + 1}: annotation does not immediately precede a fence"
                )
            try:
                attrs = dict(
                    token.split("=", 1) for token in shlex.split(annotation.group("attrs"))
                )
            except ValueError as error:
                raise QuickstartError(
                    f"line {index + 1}: invalid annotation attributes: {error}"
                ) from error
            block_id = annotation.group("id")
            if block_id in seen:
                raise QuickstartError(f"line {index + 1}: duplicate docs-validation id {block_id}")
            seen.add(block_id)
            pending = (block_id, attrs, index + 1)
            index += 1
            continue
        opened = FENCE_OPEN.match(line)
        if opened and not FENCE_CLOSE.match(line):
            if pending is None:
                raise QuickstartError(f"line {index + 1}: fence has no docs-validation annotation")
            block_id, attrs, annotation_line = pending
            pending = None
            mode = attrs.get("mode")
            if mode not in {"run", "skip"}:
                raise QuickstartError(f"line {annotation_line}: mode must be run or skip")
            if mode == "skip" and not attrs.get("reason"):
                raise QuickstartError(f"line {annotation_line}: skipped block requires reason")
            language = opened.group(1)
            if not language:
                raise QuickstartError(f"line {index + 1}: fence needs a language")
            content: list[str] = []
            index += 1
            while index < len(lines) and not FENCE_CLOSE.match(lines[index]):
                content.append(lines[index])
                index += 1
            if index == len(lines):
                raise QuickstartError(f"line {annotation_line}: unterminated fence")
            fences.append(
                Fence(
                    block_id,
                    mode,
                    language,
                    attrs.get("reason", ""),
                    "\n".join(content),
                    annotation_line,
                )
            )
        elif pending is not None and line.strip():
            raise QuickstartError(
                f"line {pending[2]}: annotation does not immediately precede a fence"
            )
        index += 1
    if pending is not None:
        raise QuickstartError(f"line {pending[2]}: annotation has no fence")
    if not fences:
        raise QuickstartError("no annotated fences found")
    return fences


def destination_for(lines: list[str], annotation_line: int) -> str:
    start = annotation_line - 1
    for index in range(start, max(-1, start - 40), -1):
        found = SAVE_AS.search(lines[index])
        if not found:
            continue
        name = found.group(1)
        if not FILENAME.fullmatch(name):
            raise QuickstartError(f"line {annotation_line}: refusing to write {name!r}")
        return name
    raise QuickstartError(f"line {annotation_line}: no `as filename` before this file fence")


def normalize_image(value: str, documented: str) -> str:
    """Accept a bare tag or a full honua-server reference. Reject anything else."""
    value = value.strip()
    if value == "":
        return documented
    if FULL_TAG.fullmatch(value) or FULL_DIGEST.fullmatch(value):
        return value
    if BARE_TAG.fullmatch(value):
        return f"ghcr.io/honua-io/honua-server:{value}"
    raise QuickstartError(
        "image must be a tag or a ghcr.io/honua-io/honua-server reference"
    )


def documented_image(fences: list[Fence]) -> str:
    found: list[str] = []
    for fence in fences:
        if fence.mode != "run" or fence.language not in {"bash", "sh"}:
            continue
        found.extend(IMAGE_LINE.findall(fence.body))
    if not found:
        raise QuickstartError("the quickstart has no HONUA_IMAGE assignment to run")
    if len(set(found)) != 1:
        raise QuickstartError("run fences disagree on HONUA_IMAGE")
    image = found[0]
    if not (FULL_TAG.fullmatch(image) or FULL_DIGEST.fullmatch(image)):
        raise QuickstartError(
            "the page's HONUA_IMAGE is not a ghcr.io/honua-io/honua-server reference"
        )
    return image


def apply_image(body: str, documented: str, requested: str) -> str:
    if requested == documented:
        return body
    needle = f"HONUA_IMAGE={documented}"
    replacement = f"HONUA_IMAGE={requested}"
    lines = [replacement if line == needle else line for line in body.splitlines()]
    if needle not in body.splitlines():
        return body
    return "\n".join(lines)


def plan_fences(text: str, image_input: str) -> tuple[list[Action], str, str, bool]:
    lines = text.splitlines()
    fences = parse_document(text)
    documented = documented_image(fences)
    requested = normalize_image(image_input, documented)
    overridden = requested != documented
    actions: list[Action] = []
    for fence in fences:
        if fence.mode == "skip":
            actions.append(Action(fence.block_id, "skip", fence.language, fence.body, "", fence.reason))
            continue
        if fence.language in {"yaml", "python"}:
            filename = destination_for(lines, fence.line)
            actions.append(Action(fence.block_id, "write", fence.language, fence.body, filename, ""))
            continue
        if fence.language in {"bash", "sh"}:
            body = apply_image(fence.body, documented, requested) if overridden else fence.body
            actions.append(Action(fence.block_id, "run", fence.language, body, "", ""))
            continue
        raise QuickstartError(
            f"line {fence.line}: cannot run {fence.language} on this Ubuntu runner"
        )
    return actions, documented, requested, overridden


def render_script(actions: list[Action]) -> str:
    """One bash script. File fences use quoted heredocs so ``${...}`` stays literal."""
    chunks = ["set -e\n"]
    for action in actions:
        if action.kind == "skip":
            continue
        if action.kind == "write":
            token = "G3_FENCE_END"
            while token in action.body:
                token += "_X"
            body = action.body.rstrip("\n") + "\n"
            chunks.append(f"cat > {shlex.quote(action.filename)} << '{token}'\n{body}{token}\n")
            chunks.append(
                f'printf \'%s\\n\' "{MARK} wrote {action.block_id} {action.filename} '
                '$(date -u +%Y-%m-%dT%H:%M:%SZ)"\n'
            )
            continue
        if action.kind == "run":
            chunks.append(
                f'printf \'%s\\n\' "{MARK} start {action.block_id} '
                '$(date -u +%Y-%m-%dT%H:%M:%SZ)"\n'
            )
            chunks.append(action.body.rstrip("\n") + "\n")
            chunks.append(
                f'printf \'%s\\n\' "{MARK} end {action.block_id} '
                '$(date -u +%Y-%m-%dT%H:%M:%SZ)"\n'
            )
            continue
        raise QuickstartError(f"unknown action {action.kind}")
    return "".join(chunks)


def parse_trace(output: str) -> dict[str, TraceEvent]:
    events: dict[str, TraceEvent] = {}
    current: str | None = None
    for line in output.splitlines():
        if not line.startswith(MARK + " "):
            if current is not None:
                events[current].output.append(line)
            continue
        parts = line[len(MARK) + 1 :].split(" ")
        kind = parts[0]
        if kind == "start" and len(parts) == 3:
            block_id, timestamp = parts[1], parts[2]
            event = events.setdefault(block_id, TraceEvent())
            event.started = timestamp
            current = block_id
            continue
        if kind == "end" and len(parts) == 3:
            block_id, timestamp = parts[1], parts[2]
            event = events.setdefault(block_id, TraceEvent())
            event.ended = timestamp
            event.complete = True
            current = block_id
            continue
        if kind == "wrote" and len(parts) == 4:
            block_id, filename, timestamp = parts[1], parts[2], parts[3]
            event = events.setdefault(block_id, TraceEvent())
            event.started = timestamp
            event.ended = timestamp
            event.filename = filename
            event.complete = True
            current = None
            continue
        if current is not None:
            events[current].output.append(line)
    return events


def default_runner(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return 124, stdout + stderr + "\nquickstart shell timed out\n"
    except OSError as error:
        return 127, str(error)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def secret_values(run_dir: Path) -> list[str]:
    env_path = run_dir / ".env"
    if not env_path.is_file():
        return []
    values: list[str] = []
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in SECRET_KEYS and len(value) >= 6:
            values.append(value)
    return values


def redact(text: str, secrets: list[str]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def _prepare_dirs(run_dir: Path, log_dir: Path) -> None:
    resolved_run = run_dir.resolve()
    resolved_log = log_dir.resolve()
    if resolved_run == Path("/") or len(resolved_run.parts) < 3:
        raise QuickstartError("refusing to use this quickstart directory")
    if resolved_run == resolved_log or resolved_run in resolved_log.parents:
        raise QuickstartError("the log directory must be outside the quickstart directory")
    if (resolved_run / ".git").exists():
        raise QuickstartError("the quickstart run directory must not be a git checkout")
    run_dir.mkdir(parents=True, exist_ok=True)
    if any(resolved_run.iterdir()):
        raise QuickstartError("the quickstart run directory must start empty")
    log_dir.mkdir(parents=True, exist_ok=True)


def execute(
    actions: list[Action],
    run_dir: Path,
    log_dir: Path,
    runner: Runner = default_runner,
    timeout: int = SHELL_TIMEOUT,
    teardown: bool = True,
    owned: dict[str, bool] | None = None,
) -> Execution:
    _prepare_dirs(run_dir, log_dir)
    if owned is not None:
        owned["yes"] = True
    script_path = (log_dir / "quickstart.sh").resolve()
    script_path.write_text(render_script(actions), encoding="utf-8")
    code, output = runner(["bash", "--noprofile", "--norc", str(script_path)], run_dir, timeout)
    (log_dir / "quickstart.log").write_text(output, encoding="utf-8")
    events = parse_trace(output)
    steps: list[Step] = []
    failed = code != 0
    seen_failure = False
    for action in actions:
        if action.kind == "skip":
            steps.append(
                Step(
                    action.block_id,
                    "skip",
                    "skipped",
                    "",
                    "",
                    None,
                    f"Skipped. {action.reason}.",
                    "",
                    action.reason,
                )
            )
            continue
        event = events.get(action.block_id)
        if seen_failure or event is None or not event.complete:
            if event is not None and event.started and not event.complete and not seen_failure:
                text = "\n".join(event.output)
                steps.append(
                    Step(
                        action.block_id,
                        action.kind,
                        "failed",
                        event.started,
                        event.started,
                        code,
                        f"Exit {code}.",
                        text,
                    )
                )
                seen_failure = True
                continue
            steps.append(Step(action.block_id, action.kind, "not_run", "", "", None, "Not run.", ""))
            continue
        text = "\n".join(event.output)
        if action.kind == "write":
            detail = f"Wrote `{event.filename or action.filename}`."
        else:
            detail = "Exit 0."
        steps.append(
            Step(
                action.block_id,
                action.kind,
                "ok",
                event.started,
                event.ended or event.started,
                0,
                detail,
                text,
            )
        )
    if failed and not seen_failure:
        steps.insert(
            0,
            Step(
                "runner",
                "run",
                "failed",
                "",
                "",
                code,
                f"Exit {code}. The quickstart shell exited before a step could finish.",
                output,
            ),
        )
    teardown_note = "not requested"
    if teardown and (run_dir / "compose.yaml").is_file():
        down_code, down_output = runner(
            ["docker", "compose", "down", "--volumes"], run_dir, TEARDOWN_TIMEOUT
        )
        (log_dir / "teardown.log").write_text(down_output, encoding="utf-8")
        teardown_note = f"exit {down_code}"
    elif teardown:
        teardown_note = "compose.yaml was not written"
    _annotate_files(run_dir, steps)
    secrets = secret_values(run_dir)
    for step in steps:
        step.output = redact(step.output, secrets)
        step.detail = redact(step.detail, secrets)
    for path in log_dir.iterdir():
        if path.is_file() and path.suffix == ".log":
            path.write_text(
                redact(path.read_text(encoding="utf-8", errors="replace"), secrets),
                encoding="utf-8",
            )
    for step in steps:
        if step.output:
            (log_dir / f"{step.block_id}.log").write_text(step.output, encoding="utf-8")
    return Execution(steps, code, teardown_note)


def _annotate_files(run_dir: Path, steps: list[Step]) -> None:
    for step in steps:
        if step.status != "ok":
            continue
        if step.block_id == "install.compose":
            compose = run_dir / "compose.yaml"
            if compose.is_file() and "platform: linux/amd64" in compose.read_text(encoding="utf-8"):
                step.detail += " `platform: linux/amd64` is in the documented service."
        elif step.block_id == "install.secrets":
            step.detail += " `.env` created. Secrets are not in this record."
        elif step.block_id == "install.keyring" and (run_dir / "secrets" / "keyring.pfx").is_file():
            step.detail += " `secrets/keyring.pfx` created."


def conclude(steps: list[Step], overridden: bool) -> tuple[bool, bool, str]:
    """A pass needs every run fence and the documented ``west`` / ``east`` query."""
    query = next(
        (step for step in steps if step.block_id == "install.query" and step.kind != "skip"),
        None,
    )
    failed = next((step for step in steps if step.status == "failed"), None)
    if failed is not None:
        return False, False, f"`{failed.block_id}` {failed.detail}"
    if any(step.status == "not_run" for step in steps):
        pending = next(step for step in steps if step.status == "not_run")
        return False, False, f"`{pending.block_id}` was not run."
    if query is None or query.status != "ok":
        return False, False, (
            "The page has no `install.query` run fence, so the documented query did not run."
        )
    if not all(word in query.output for word in QUERY_WORDS):
        query.status = "failed"
        query.detail = "Exit 0, but the output did not include the documented features west and east."
        return False, False, f"`install.query` {query.detail}"
    return True, not overridden, ""


def parse_compose_version(text: str) -> tuple[int, int, int] | None:
    match = COMPOSE_VERSION.search(text)
    if match is None:
        return None
    return int(match.group("major")), int(match.group("minor")), int(match.group("patch"))


def host_preflight(compose_text: str) -> str | None:
    parsed = parse_compose_version(compose_text)
    if parsed is not None and parsed < MIN_COMPOSE:
        found = ".".join(str(part) for part in parsed)
        return f"Docker Compose {found} is older than the quickstart's 2.23.1."
    return None


def command_text(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if completed.returncode != 0:
        return "unavailable"
    text = (completed.stdout or completed.stderr).strip()
    return text.splitlines()[0] if text else "unavailable"


def ghcr_manifest_status(image: str) -> str:
    if FULL_TAG.fullmatch(image):
        reference = image.split(":", 1)[1]
    elif FULL_DIGEST.fullmatch(image):
        reference = image.split("@", 1)[1]
    else:
        return "image is not a ghcr.io/honua-io/honua-server reference"
    name = "honua-io/honua-server"
    token_url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{name}:pull"
    try:
        with urllib.request.urlopen(token_url, timeout=20) as response:
            token = json.load(response)["token"]
        request = urllib.request.Request(
            f"https://ghcr.io/v2/{name}/manifests/{reference}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": ", ".join(
                    [
                        "application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.docker.distribution.manifest.v2+json",
                    ]
                ),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return f"HTTP {response.status}"
        except urllib.error.HTTPError as error:
            body = " ".join(error.read(180).decode("utf-8", "replace").split())
            return f"HTTP {error.code} {body}".strip()
    except Exception as error:  # noqa: BLE001 — the probe is evidence, not the gate
        return f"probe failed ({error.__class__.__name__})"


def local_image_digest(image: str) -> str:
    code, output = default_runner(
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image],
        Path.cwd(),
        30,
    )
    if code != 0:
        return ""
    try:
        digests = json.loads(output.strip() or "[]")
    except json.JSONDecodeError:
        return ""
    if isinstance(digests, list) and digests:
        return str(digests[0])
    return ""


def _clock(timestamp: str) -> str:
    if "T" in timestamp and timestamp.endswith("Z"):
        return timestamp.split("T", 1)[1]
    return timestamp or "—"


def _duration(start: str, end: str) -> str:
    if not start or not end:
        return "—"
    try:
        began = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
        finished = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return "—"
    seconds = int((finished - began).total_seconds())
    if seconds < 1:
        return "under 1s"
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m{remainder:02d}s"


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ").strip()


def _excerpt(output: str) -> str:
    text = "\n".join(output.splitlines()[-40:])
    if len(text) > 4000:
        text = text[-4000:]
    return text.replace("```", "'''")


def source_url(sha: str) -> str:
    if COMMIT.fullmatch(sha):
        return (
            "https://github.com/honua-io/honua-server/blob/"
            f"{sha}/docs/get-started/quickstart.md"
        )
    return "honua-server `docs/get-started/quickstart.md`"


def receipt_filename(started: str, run_id: str, run_attempt: str) -> str:
    if not STARTED.fullmatch(started):
        raise QuickstartError("start time must be UTC")
    if not RUN_NUMBER.fullmatch(run_id) or not RUN_NUMBER.fullmatch(run_attempt):
        raise QuickstartError("run id must be numeric")
    return f"G3-clean-machine-quickstart-{started[:10]}-run-{run_id}-{run_attempt}.md"


def render_receipt(
    *,
    started: str,
    finished: str,
    runner: str,
    run_id: str,
    run_attempt: str,
    os_text: str,
    arch: str,
    docker: str,
    compose: str,
    python: str,
    openssl: str,
    source_sha: str,
    documented_image: str,
    image: str,
    overridden: bool,
    passed: bool,
    gate_green: bool,
    failure: str,
    steps: list[Step],
    registry_note: str,
    digest: str,
    teardown: str,
) -> str:
    if overridden:
        intro = (
            "Launch gate for honua-release#1009. The public Docker Compose quickstart "
            "was followed as written, except the `HONUA_IMAGE` line. This dispatch set "
            f"it to `{image}` instead of the page's `{documented_image}`."
        )
    else:
        intro = (
            "Launch gate for honua-release#1009. The public Docker Compose quickstart "
            "was followed as written."
        )
    if gate_green:
        result = (
            "**Passed.** The public Compose quickstart finished on this runner. "
            "The documented fences ran, and the query printed the two `places` features "
            f"(`west` and `east`). This record is the G3 evidence for that procedure on "
            f"`{documented_image}`. The first-map page was not part of this dispatch."
        )
    elif passed:
        result = (
            f"**Passed for `{image}` only.** The documented fences ran with that one "
            "`HONUA_IMAGE` change, and the query printed the two `places` features. "
            "This record does not turn G3 green for the documented channel tag. "
            "The first-map page was not part of this dispatch."
        )
    else:
        result = f"**Failed.** {failure} This record does not turn G3 green."
    machine = (
        f"{runner}, {arch}. Run {run_id}, attempt {run_attempt}. "
        "The quickstart directory was created empty and was not a git checkout."
    )
    if runner.startswith("GitHub-hosted"):
        machine += (
            " honua-release was checked out in another directory so this receipt "
            "could be committed."
        )
    day = started[:10]
    if finished[:10] == day:
        when = f"{day}, {_clock(started)} to {_clock(finished)}"
    else:
        when = f"{started} to {finished}"
    rows = [
        "| Field | Value |",
        "| --- | --- |",
        f"| Date | {_cell(when)} |",
        f"| OS | {_cell(os_text)} |",
        f"| Docker | {_cell(docker)} |",
        f"| Compose | {_cell(compose)} |",
        f"| Python | {_cell(python)} |",
        f"| OpenSSL | {_cell(openssl)} |",
        "| Run directory | A new empty directory, not a git checkout |",
        f"| Machine | {_cell(machine)} |",
        f"| Quickstart | {_cell(source_url(source_sha))} |",
        f"| Image | {_cell(image)} |",
    ]
    if overridden:
        rows.append(f"| Documented image | {_cell(documented_image)} |")
    step_intro = (
        f"Source: {source_url(source_sha)}. Bash fences were copied unchanged into one "
        "shell and executed with `bash --noprofile --norc` and `set -e`, so a failed "
        "command stops the step and the virtualenv activation persists. `mode=skip` "
        "fences were not run. After those fences, the harness runs "
        "`docker compose down --volumes` when `compose.yaml` exists."
    )
    if overridden:
        step_intro += " The secrets fence differs by the `HONUA_IMAGE` line only."
    table = [
        "| Step | UTC | Duration | Outcome |",
        "| --- | --- | --- | --- |",
    ]
    if not steps:
        table.append("| — | — | — | No quickstart fence ran. |")
    for step in steps:
        if step.status == "skipped":
            utc, duration = "—", "—"
        elif step.status == "not_run" or not step.started:
            utc, duration = "—", "—"
        else:
            start = _clock(step.started)
            end = _clock(step.ended) if step.ended else start
            utc = start if start == end else f"{start}–{end}"
            duration = _duration(step.started, step.ended or step.started)
        table.append(
            f"| `{step.block_id}` | {utc} | {duration} | {_cell(step.detail)} |"
        )
    closing_heading = "## Record" if passed else "## Defect"
    closing = [
        "This run did not substitute the pre-cut rehearsal digest in "
        "`customer-install-manifest.json`.",
        "",
        f"Harness teardown: {teardown}.",
    ]
    if passed:
        if digest:
            closing.insert(0, f"Local image digest: `{digest}`.")
        else:
            closing.insert(0, "The local image digest was not recorded.")
        closing.insert(1, "")
    else:
        failed_step = next((step for step in steps if step.status == "failed"), None)
        if failed_step is not None and failed_step.output.strip():
            closing[0:0] = ["```", _excerpt(failed_step.output), "```", ""]
        if registry_note:
            closing.insert(0, f"An anonymous GHCR read of the dispatched reference returned {registry_note}.")
            closing.insert(1, "")
    lines = [
        f"# G3 clean-machine quickstart run — {day}",
        "",
        intro,
        "",
        "## Result",
        "",
        result,
        "",
        "## Host",
        "",
        *rows,
        "",
        "## Steps",
        "",
        step_intro,
        "",
        *table,
        "",
        closing_heading,
        "",
        *closing,
        "",
    ]
    return "\n".join(lines)


def pr_title(passed: bool, gate_green: bool, run_id: str) -> str:
    if gate_green:
        return f"docs: record the G3 compose quickstart run {run_id}"
    if passed:
        return f"docs: record a G3 quickstart run against an override image ({run_id})"
    return f"docs: record the failed G3 compose quickstart run {run_id}"


def render_pr_body(
    *,
    run_id: str,
    source_sha: str,
    image: str,
    passed: bool,
    gate_green: bool,
    receipt: str,
) -> str:
    if gate_green:
        outcome = (
            "Result: passed on the documented image. "
            "This receipt is the G3 evidence for the public Compose quickstart."
        )
    elif passed:
        outcome = (
            "Result: passed for an override image. "
            "This record does not turn G3 green for the documented channel tag."
        )
    else:
        outcome = "Result: failed. This record does not turn G3 green."
    return "\n".join(
        [
            "## Summary",
            "",
            f"- Actions run {run_id} followed the public Compose quickstart at `{source_sha}` "
            "on a fresh `ubuntu-latest` runner.",
            f"- Image: `{image}`.",
            f"- {outcome}",
            "",
            f"The receipt is `{receipt}`.",
            "",
            "Trunk requires a pull request, so this dispatch opens the record and does not "
            "merge it. The first-map page was not part of the run.",
            "",
            "Refs #1009 (accepting a passing receipt for the documented image is what remains; "
            "a failed run does not close the issue)",
            "",
        ]
    )


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def remove_run_dir(path: Path) -> None:
    resolved = path.resolve()
    if resolved == Path("/") or len(resolved.parts) < 3:
        raise QuickstartError("refusing to remove the run directory")
    if (resolved / ".git").exists():
        raise QuickstartError("refusing to remove a git checkout")
    if resolved.exists():
        shutil.rmtree(resolved)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_outputs(
    *,
    args: argparse.Namespace,
    started: str,
    finished: str,
    os_text: str,
    arch: str,
    docker: str,
    compose: str,
    python: str,
    openssl: str,
    documented: str,
    image: str,
    overridden: bool,
    passed: bool,
    gate_green: bool,
    failure: str,
    steps: list[Step],
    registry_note: str,
    digest: str,
    teardown: str,
) -> Path:
    if not COMMIT.fullmatch(args.source_sha):
        raise QuickstartError("source sha must be the 40-character honua-server commit")
    args.receipt_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    filename = receipt_filename(started, args.run_id, args.run_attempt)
    receipt_path = args.receipt_dir / filename
    text = render_receipt(
        started=started,
        finished=finished,
        runner=args.runner,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        os_text=os_text,
        arch=arch,
        docker=docker,
        compose=compose,
        python=python,
        openssl=openssl,
        source_sha=args.source_sha,
        documented_image=documented,
        image=image,
        overridden=overridden,
        passed=passed,
        gate_green=gate_green,
        failure=failure,
        steps=steps,
        registry_note=registry_note,
        digest=digest,
        teardown=teardown,
    )
    receipt_path.write_text(text, encoding="utf-8")
    relative = _display_path(receipt_path)
    (args.log_dir / "pr-title.txt").write_text(
        pr_title(passed, gate_green, args.run_id) + "\n", encoding="utf-8"
    )
    (args.log_dir / "pr-body.md").write_text(
        render_pr_body(
            run_id=args.run_id,
            source_sha=args.source_sha,
            image=image,
            passed=passed,
            gate_green=gate_green,
            receipt=relative,
        ),
        encoding="utf-8",
    )
    outcome = {
        "status": "passed" if passed else "failed",
        "gate_green": gate_green,
        "image": image,
        "documented_image": documented,
        "overridden": overridden,
        "source_sha": args.source_sha,
        "receipt": relative,
        "run_id": args.run_id,
    }
    (args.log_dir / "outcome.json").write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    return receipt_path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quickstart", type=Path, required=True)
    parser.add_argument("--image", default="2026.1-rc")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--runner", default="local")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", default="1")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip host probes, registry reads, and teardown. Tests only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = _utc_now()
    os_text = arch = docker = compose = python = openssl = "not collected"
    if not args.offline:
        os_text = command_text(["uname", "-sr"])
        arch = command_text(["uname", "-m"])
        docker = command_text(["docker", "version", "--format", "{{.Server.Version}}"])
        compose = command_text(["docker", "compose", "version"])
        python = command_text(["python", "--version"])
        openssl = command_text(["openssl", "version"])
    documented = image = ""
    overridden = False
    owned: dict[str, bool] = {"yes": False}
    try:
        page = args.quickstart.read_text(encoding="utf-8")
        actions, documented, image, overridden = plan_fences(page, args.image)
        preflight = None if args.offline else host_preflight(compose)
        if preflight:
            _write_outputs(
                args=args,
                started=started,
                finished=_utc_now(),
                os_text=os_text,
                arch=arch,
                docker=docker,
                compose=compose,
                python=python,
                openssl=openssl,
                documented=documented,
                image=image,
                overridden=overridden,
                passed=False,
                gate_green=False,
                failure=preflight,
                steps=[],
                registry_note="",
                digest="",
                teardown="not run",
            )
            print("g3: failed", flush=True)
            return 1
        execution = execute(
            actions,
            args.run_dir,
            args.log_dir,
            teardown=not args.offline,
            owned=owned,
        )
        passed, gate_green, failure = conclude(execution.steps, overridden)
        registry_note = ""
        digest = ""
        if not args.offline and not passed:
            registry_note = ghcr_manifest_status(image)
        if not args.offline and passed:
            digest = local_image_digest(image)
        _write_outputs(
            args=args,
            started=started,
            finished=_utc_now(),
            os_text=os_text,
            arch=arch,
            docker=docker,
            compose=compose,
            python=python,
            openssl=openssl,
            documented=documented,
            image=image,
            overridden=overridden,
            passed=passed,
            gate_green=gate_green,
            failure=failure,
            steps=execution.steps,
            registry_note=registry_note,
            digest=digest,
            teardown=execution.teardown,
        )
        print("g3: passed" if passed else "g3: failed", flush=True)
        return 0 if passed else 1
    except (OSError, QuickstartError) as error:
        try:
            _write_outputs(
                args=args,
                started=started,
                finished=_utc_now(),
                os_text=os_text,
                arch=arch,
                docker=docker,
                compose=compose,
                python=python,
                openssl=openssl,
                documented=documented or "(unread)",
                image=image or args.image,
                overridden=overridden,
                passed=False,
                gate_green=False,
                failure=str(error),
                steps=[],
                registry_note="",
                digest="",
                teardown="not run",
            )
        except QuickstartError:
            print("g3: failed", flush=True)
            return 2
        print("g3: failed", flush=True)
        return 1
    finally:
        # The key ring file is root-owned after Compose starts. Leaving it on a
        # disposable runner is safer than failing a finished run for the cleanup.
        if owned["yes"] and args.run_dir.exists():
            try:
                remove_run_dir(args.run_dir)
            except (OSError, QuickstartError):
                print("g3: could not remove the run directory", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
