"""Allow-listed, recorded shell for the local Docker setup stage of the AI arc.

The customer's local install is the published Compose quickstart run in a terminal. The agent gets
that terminal as three MCP-shaped tools over one scratch directory on the arc's Docker host:

  run_command  argv only (no shell syntax): docker compose, curl to loopback, openssl for the
               quickstart's secrets and key ring, and a few file utilities on relative paths
  write_file   write a file under the scratch directory (0600)
  read_file    read a file under the scratch directory

Nothing else runs. ``docker compose`` is always pinned to the arc's own project (the harness adds
``-p``); exec/run/cp, other projects, other contexts and other hosts are refused, so the agent has
no access to other containers or the database. ``curl`` may only name http(s) URLs on loopback.
Every call, refused or executed, is written to the runner's action log before it runs.
"""
from __future__ import annotations

import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

MAX_FILE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
COMPOSE_SUBCOMMANDS = {'up', 'down', 'ps', 'logs', 'pull', 'config', 'start', 'stop', 'restart', 'images',
                       'version', 'top', 'events', 'wait'}
COMPOSE_REFUSED_OPTIONS = {'-p', '--project-name', '--project-directory', '-H', '--host', '--context', '-c',
                           '--env-file', '--profile', '--ansi'}
CURL_FLAGS = {'-s', '--silent', '-S', '--show-error', '-f', '--fail', '-i', '--include', '-I', '--head',
              '-v', '--verbose', '-sS', '-fsS', '-sf', '-fs', '-k', '--insecure', '-sSf', '-g', '--globoff'}
CURL_VALUED = {'-X', '--request', '-H', '--header', '-d', '--data', '--data-raw', '--json', '-u', '--user',
               '--max-time', '-m', '-w', '--write-out', '-o', '--output', '--connect-timeout', '--retry'}
LOOPBACK = {'localhost', '127.0.0.1', '::1'}
OPENSSL = {'rand', 'req', 'pkcs12'}
FILE_UTILS = {'mkdir', 'rm', 'chmod', 'cat', 'ls', 'grep', 'cut', 'cp', 'mv', 'head', 'tail', 'wc'}
SAFE_TOKEN = re.compile(r'\A[^\x00\n\r]{0,4096}\Z')
PROJECT = re.compile(r'\Aarc-[a-z0-9]{6,32}\Z')

TOOL_VIEW = {'tools': [
    {'name': 'run_command',
     'description': ('Run one command in the install folder on the Docker host, as a list of arguments '
                     '(no shell features such as pipes, redirection or variable expansion). Admitted: docker '
                     'compose for this install, curl to this machine, openssl, and basic file commands on '
                     'paths inside the folder.'),
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['argv'],
                     'properties': {'argv': {'type': 'array', 'minItems': 1, 'maxItems': 64,
                                             'items': {'type': 'string', 'maxLength': 4096}},
                                    'timeoutSeconds': {'type': 'integer', 'minimum': 1, 'maximum': 900}}}},
    {'name': 'write_file',
     'description': 'Create or replace a file inside the install folder. The file is private to its owner.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['path', 'content'],
                     'properties': {'path': {'type': 'string', 'maxLength': 200},
                                    'content': {'type': 'string', 'maxLength': MAX_FILE_BYTES}}}},
    {'name': 'read_file',
     'description': 'Read a text file inside the install folder.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['path'],
                     'properties': {'path': {'type': 'string', 'maxLength': 200}}}}]}


class Refused(ValueError):
    """The requested action is outside the allow-list; it is recorded and never executed."""


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or not SAFE_TOKEN.fullmatch(value):
        raise Refused('path must be a non-empty relative path')
    if value.startswith(('/', '~', '-')) or '\\' in value:
        raise Refused('path must be relative to the install folder')
    norm = posixpath.normpath(value)
    if norm == '..' or norm.startswith('../') or '/../' in f'/{norm}/' or norm.startswith('/'):
        raise Refused('path leaves the install folder')
    return norm


def _loopback_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return parts.scheme in ('http', 'https') and (parts.hostname or '').lower() in LOOPBACK
    except ValueError:
        return False


def check_compose(args: list[str]) -> list[str]:
    files, rest, i = [], [], 0
    while i < len(args):
        arg = args[i]
        name = arg.split('=', 1)[0]
        if name in COMPOSE_REFUSED_OPTIONS:
            raise Refused('the compose project, host and context belong to this install')
        if name in ('-f', '--file'):
            value = arg.split('=', 1)[1] if '=' in arg else (args[i + 1] if i + 1 < len(args) else '')
            files.append(relative_path(value))
            i += 1 if '=' in arg else 2
            continue
        rest = args[i:]
        break
    if not rest or rest[0] not in COMPOSE_SUBCOMMANDS:
        raise Refused('compose subcommand not admitted (exec, run, cp and listing other projects are refused)')
    for token in rest[1:]:
        value = token.split('=', 1)[1] if token.startswith('--') and '=' in token else token
        if value.startswith(('/', '~')) or '..' in value.split('/'):
            raise Refused('paths outside the install folder are not admitted')
    return [arg for f in files for arg in ('-f', f)] + rest


def check_curl(args: list[str]) -> None:
    urls, i = 0, 0
    while i < len(args):
        arg = args[i]
        if arg in CURL_FLAGS:
            i += 1
            continue
        name = arg.split('=', 1)[0]
        if name in CURL_VALUED:
            value = arg.split('=', 1)[1] if (arg.startswith('--') and '=' in arg) else (
                args[i + 1] if i + 1 < len(args) else None)
            if value is None:
                raise Refused('curl option without a value')
            if name in ('-o', '--output'):
                relative_path(value)
            if name in ('-d', '--data', '--data-raw', '--json') and value.startswith('@'):
                relative_path(value[1:])
            i += 1 if (arg.startswith('--') and '=' in arg) else 2
            continue
        if arg.startswith('-'):
            raise Refused('curl option not admitted: ' + arg[:40])
        if not _loopback_url(arg):
            raise Refused('curl may only reach this machine (localhost)')
        urls += 1
        i += 1
    if urls == 0:
        raise Refused('curl needs a localhost URL')


def check_openssl(args: list[str]) -> None:
    if not args or args[0] not in OPENSSL:
        raise Refused('openssl is admitted for rand, req and pkcs12 only')
    for k, arg in enumerate(args[1:]):
        prev = args[k] if k else ''
        if prev in ('-out', '-keyout', '-in', '-inkey', '-config'):
            relative_path(arg)


def check_file_util(command: str, args: list[str]) -> None:
    operands = []
    for arg in args:
        if arg.startswith('-') and arg != '-':
            if '=' in arg:
                value = arg.split('=', 1)[1]
                if '/' in value or value.startswith('~'):
                    relative_path(value)
            continue
        operands.append(arg)
    if command in ('grep', 'chmod') and operands:
        if command == 'chmod' and not re.fullmatch(r'[0-7]{3,4}|[ugoa]*[-+=][rwxX]+', operands[0]):
            raise Refused('chmod mode not admitted')
        operands = operands[1:]  # the pattern or the mode
    for operand in operands:
        if relative_path(operand) == '.' and command in ('rm', 'mv'):
            raise Refused('removing or moving the install folder itself is not admitted')


def check_command(argv: list, project: str) -> list[str]:
    """Return the argv to execute, or raise Refused. ``project`` is the harness-owned compose project."""
    if not PROJECT.fullmatch(project):
        raise ValueError('invalid harness compose project')
    if (not isinstance(argv, list) or not argv or len(argv) > 64
            or any(not isinstance(a, str) or not SAFE_TOKEN.fullmatch(a) for a in argv)):
        raise Refused('argv must be a list of plain strings')
    command, args = argv[0], argv[1:]
    if command == 'docker':
        if not args or args[0] != 'compose':
            raise Refused('only docker compose is admitted')
        return ['docker', 'compose', '-p', project, *check_compose(args[1:])]
    if command == 'curl':
        check_curl(args)
        return ['curl', *args]
    if command == 'openssl':
        check_openssl(args)
        return ['openssl', *args]
    if command in FILE_UTILS:
        check_file_util(command, args)
        return [command, *args]
    raise Refused('command not admitted: ' + command[:40])


@dataclass
class HostResult:
    returncode: int
    stdout: str
    stderr: str


class LocalHost:
    """Run admitted argv in a scratch directory on this machine (development rehearsal only)."""
    kind = 'local'

    def __init__(self, scratch: Path, run: Callable = subprocess.run):
        self.scratch, self._run = Path(scratch), run

    def run(self, argv: list[str], timeout: int) -> HostResult:
        done = self._run(argv, cwd=str(self.scratch), capture_output=True, text=True, timeout=timeout,
                         check=False, env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(self.scratch)})
        return HostResult(done.returncode, done.stdout or '', done.stderr or '')

    def write(self, path: str, content: str) -> None:
        target = self.scratch / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        target.chmod(0o600)

    def read(self, path: str) -> str:
        return (self.scratch / path).read_text(encoding='utf-8', errors='replace')


class SshHost:
    """Run admitted argv in a scratch directory on the arc's ephemeral Docker host over SSH.

    ``target`` is ``user@host``; the runner owns the key and known_hosts entry. Arguments are
    quoted with shlex, so the agent's strings never become shell syntax on the far side.
    """
    kind = 'ssh'

    def __init__(self, target: str, scratch: str, *, ssh_options=(), run: Callable = subprocess.run):
        if not re.fullmatch(r'[A-Za-z0-9._-]+@[A-Za-z0-9.-]+', target) or not scratch.startswith('/'):
            raise ValueError('invalid SSH target or remote scratch path')
        self.target, self.scratch, self.options, self._run = target, scratch, list(ssh_options), run

    def _ssh(self, remote: str, timeout: int, stdin: str | None = None) -> HostResult:
        done = self._run(['ssh', '-o', 'BatchMode=yes', *self.options, self.target, remote], input=stdin,
                         capture_output=True, text=True, timeout=timeout, check=False)
        return HostResult(done.returncode, done.stdout or '', done.stderr or '')

    def run(self, argv: list[str], timeout: int) -> HostResult:
        return self._ssh(f'cd {shlex.quote(self.scratch)} && umask 077 && {shlex.join(argv)}', timeout)

    def write(self, path: str, content: str) -> None:
        quoted = shlex.quote(posixpath.join(self.scratch, path))
        result = self._ssh(f'umask 077 && mkdir -p "$(dirname {quoted})" && cat > {quoted}', 60, content)
        if result.returncode:
            raise RuntimeError('remote write failed')

    def read(self, path: str) -> str:
        result = self._ssh(f'cat {shlex.quote(posixpath.join(self.scratch, path))}', 60)
        if result.returncode:
            raise FileNotFoundError(path)
        return result.stdout


def clip(text: str) -> str:
    raw = text.encode('utf-8', errors='replace')
    if len(raw) <= MAX_OUTPUT_BYTES:
        return text
    return raw[:MAX_OUTPUT_BYTES].decode('utf-8', errors='ignore') + '\n[output truncated]'


class ShellSurface:
    """The setup-stage tool surface. ``execute`` is what the session's recording proxy dispatches to."""

    def __init__(self, host, project: str):
        self.host, self.project = host, project
        self.tool_view = TOOL_VIEW
        self.refusals: list[dict] = []
        self.executed: list[list[str]] = []

    def execute(self, kind: str, action: dict) -> dict:
        if kind != 'tool_call':
            return {'status': 'fail', 'result': {'error': 'only tool calls are admitted'}}
        tool, arguments = action.get('tool'), action.get('arguments')
        try:
            if not isinstance(arguments, dict):
                raise Refused('arguments must be an object')
            allowed = {t['name']: set(t['inputSchema']['properties']) for t in TOOL_VIEW['tools']}
            if tool not in allowed or set(arguments) - allowed[tool]:
                raise Refused('unknown tool or argument')
            if tool == 'run_command':
                timeout = arguments.get('timeoutSeconds', 300)
                if type(timeout) is not int or not 1 <= timeout <= 900:
                    raise Refused('timeoutSeconds must be 1..900')
                argv = check_command(arguments.get('argv'), self.project)
                self.executed.append(argv)
                try:
                    done = self.host.run(argv, timeout)
                except subprocess.TimeoutExpired:
                    return {'status': 'fail', 'result': {'exitCode': None, 'error': 'timed out'}}
                return {'status': 'ok' if done.returncode == 0 else 'fail',
                        'result': {'exitCode': done.returncode, 'stdout': clip(done.stdout),
                                   'stderr': clip(done.stderr)}}
            path = relative_path(arguments.get('path'))
            if tool == 'write_file':
                content = arguments.get('content')
                if not isinstance(content, str) or len(content.encode()) > MAX_FILE_BYTES:
                    raise Refused('content must be text up to 256 KiB')
                self.host.write(path, content)
                return {'status': 'ok', 'result': {'written': path, 'bytes': len(content.encode())}}
            return {'status': 'ok', 'result': {'path': path, 'content': clip(self.host.read(path))}}
        except Refused as exc:
            self.refusals.append({'tool': tool, 'reason': str(exc)})
            return {'status': 'fail', 'refusal': 'not-admitted', 'result': {'error': str(exc)}}
        except (FileNotFoundError, IsADirectoryError):
            return {'status': 'fail', 'result': {'error': 'no such file'}}
