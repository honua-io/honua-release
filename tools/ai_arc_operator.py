"""The honua-devops operator for AWS arc setup, installed and run the customer way.

The customer downloads the pinned release archive (honua-devops v2026.1.1), checks it against the
release's ``.sha256``, and registers ``Honua.DevOps.Agent --mcp`` with their MCP client:

  claude mcp add honua-devops -- <dir>/Honua.DevOps.Agent --mcp
  codex mcp add honua-devops -- <dir>/Honua.DevOps.Agent --mcp

In the arc, the agent CLI's only MCP servers are #1160's recording proxies, so every call is in the
runner's action log. The runner therefore starts the verified binary itself, exactly as that
registration would (same executable, same ``--mcp`` argument, stdio transport), through #1160's
``StdioMcpBackend`` (bounded reads, credential screening, audit log); the proxy serves the
operator's own ``tools/list``. The registration command is recorded in the receipt.

The operator process gets slot-scoped, short-lived AWS credentials (STS session credentials) in
its own environment from the runner. The model never sees them: they are not in any tool view,
argument or result the runner returns, and any appearance of them in agent-controlled text is an
unsafe attempt.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import Path
from typing import Callable

BINARY = 'Honua.DevOps.Agent'
SHORT_LIVED_KEY = re.compile(r'\AASIA[0-9A-Z]{12,}\Z')


class OperatorError(RuntimeError):
    pass


def pinned_asset(spec: dict, platform: str = 'linux-x64') -> dict:
    asset = (spec.get('assets') or {}).get(platform)
    if not asset or not re.fullmatch(r'[0-9a-f]{64}', asset.get('sha256', '')):
        raise OperatorError('operator release asset is not pinned for ' + platform)
    return asset


def registration_command(agent: str, binary: Path) -> list[str]:
    """The customer's registration for this agent's MCP client (recorded, not executed)."""
    client = {'claude-cli': 'claude', 'codex-cli': 'codex'}.get(agent)
    if client is None:
        raise OperatorError('no MCP registration for agent ' + agent)
    return [client, 'mcp', 'add', 'honua-devops', '--', str(binary), '--mcp']


def install(spec: dict, dest: Path, *, platform: str = 'linux-x64',
            download: Callable[[str, str, str, Path], None] | None = None) -> dict:
    """Download the pinned archive and its .sha256, verify both against the arc spec, extract."""
    asset = pinned_asset(spec, platform)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)

    def gh(repo, tag, pattern, out):
        subprocess.run(['gh', 'release', 'download', tag, '--repo', repo, '--pattern', pattern, '--dir', str(out),
                        '--clobber'], check=True, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
    fetch = download or gh
    archive = dest / asset['name']
    for name in (asset['name'], asset['name'] + '.sha256'):
        fetch(spec['repo'], spec['tag'], name, dest)
    published = (dest / (asset['name'] + '.sha256')).read_text().split()
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if not published or published[0] != asset['sha256'] or actual != asset['sha256']:
        raise OperatorError('operator archive does not match the pinned sha256')
    root = dest / 'operator'
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if not str(target).startswith(str(root.resolve())) or member.issym() or member.islnk():
                raise OperatorError('operator archive has an unsafe member')
        tar.extractall(root, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
    binary = next((p for p in root.rglob(BINARY) if p.is_file()), None)
    if binary is None:
        raise OperatorError('operator archive has no ' + BINARY)
    binary.chmod(0o700)
    return {'repo': spec['repo'], 'tag': spec['tag'], 'commit': spec.get('commit'), 'asset': asset['name'],
            'archiveSha256': 'sha256:' + actual, 'binary': str(binary),
            'binarySha256': 'sha256:' + hashlib.sha256(binary.read_bytes()).hexdigest()}


def slot_credentials(path: str | None) -> dict:
    """Slot-scoped STS credentials from the slot provisioner. Long-lived keys are refused."""
    if not path:
        raise OperatorError('the slot provisioner supplied no operator credentials')
    data = json.loads(Path(path).read_text())
    env = {k: data.get(k) for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_REGION')}
    if not all(env[k] for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')) \
            or not SHORT_LIVED_KEY.fullmatch(env['AWS_ACCESS_KEY_ID']):
        raise OperatorError('operator credentials must be short-lived STS session credentials')
    if not data.get('expiresAt'):
        raise OperatorError('operator credentials must carry their expiry')
    return {k: v for k, v in env.items() if v}
