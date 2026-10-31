"""The honua-devops operator for AWS arc setup, installed and run the customer way.

The customer downloads the pinned release archive (honua-devops v2026.1.2), checks it against the
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


CONTRACT_VARIABLE = 'HONUA_DEVOPS_DEPLOY_IDENTITY_CONTRACT_PATH'
ROLE_ARN = re.compile(r'\Aarn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/(?:[A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]+\Z')


def deploy_identity(path: str | None) -> dict:
    """The administrator's honua-iac ``deploy_identity_contract`` export (bootstrap/aws-exec-identity).

    Checked here as the operator will check it (v1 kind, exact role ARN, known compute topology, no
    long-lived-credential actions) so a bad contract is a harness fault before the model starts.
    """
    if not path or not Path(path).is_absolute() or not Path(path).is_file():
        raise OperatorError('the deploy identity contract export is missing (absolute path required)')
    raw = Path(path).read_bytes()
    if len(raw) > 262144:
        raise OperatorError('deploy identity contract is too large')
    doc = json.loads(raw)
    if doc.get('schema_version') != 'v1' or doc.get('kind') != 'honua.iac.deploy-identity' \
            or not ROLE_ARN.fullmatch(str(doc.get('role_arn'))):
        raise OperatorError('expected the honua-iac deploy_identity_contract v1 output for an exact role')
    topology = doc.get('topology') or {}
    if topology.get('compute') not in ('ecs', 'eks', 'lambda') or any(
            not isinstance(topology.get(flag), bool) for flag in ('redis', 'custom_domain', 'waf', 'gp_batch')):
        raise OperatorError('deploy identity contract names no complete topology')
    actions = [a for values in (doc.get('actions_by_service') or {}).values() for a in values]
    if not actions or any(re.fullmatch(r'iam:(CreateUser|CreateAccessKey|CreateLoginProfile|'
                                       r'CreateServiceSpecificCredential)', a, re.I) for a in actions):
        raise OperatorError('deploy identity contract must list exact actions and no long-lived-credential actions')
    return {'path': str(Path(path).resolve()), 'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest(),
            'roleArn': doc['role_arn'], 'topology': topology, 'actionCount': len(actions)}


def operator_environment(contract: dict, credentials: dict) -> dict:
    """The operator's environment: the contract path plus a session for that contract's role only."""
    if credentials.get('roleArn') not in (None, contract['roleArn']):
        raise OperatorError("operator session is not for the deploy identity contract's role")
    env = {k: v for k, v in credentials.items() if k.startswith('AWS_')}
    env[CONTRACT_VARIABLE] = contract['path']
    return env


def slot_credentials(path: str | None) -> dict:
    """Slot-scoped STS credentials from the slot provisioner. Long-lived keys are refused."""
    if not path:
        raise OperatorError('the slot provisioner supplied no operator credentials')
    data = json.loads(Path(path).read_text())
    env = {k: data.get(k) for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_REGION')}
    role = data.get('roleArn')
    if not all(env[k] for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')) \
            or not SHORT_LIVED_KEY.fullmatch(env['AWS_ACCESS_KEY_ID']):
        raise OperatorError('operator credentials must be short-lived STS session credentials')
    if not data.get('expiresAt'):
        raise OperatorError('operator credentials must carry their expiry')
    out = {k: v for k, v in env.items() if v}
    if role:
        out['roleArn'] = role
    return out
