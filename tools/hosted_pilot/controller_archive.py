"""Bounded immutable archive transport for the private trusted controller."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

CONTRACT_PATHS = (
    'certification/hosted-pilot/controller/Dockerfile',
    'certification/hosted-pilot/controller/requirements.txt',
    'certification/hosted-pilot/controller/browser/package.json',
    'certification/hosted-pilot/controller/browser/package-lock.json',
    'certification/hosted-pilot/controller/browser/copy-canonical.mjs',
    'certification/hosted-pilot/controller/browser/smoke.mjs',
    'certification/hosted-pilot/controller/rds-global-bundle.pem',
    'tools/hosted_pilot/controller_archive.py',
    'tools/hosted_pilot/controller_budget.py',
    'tools/hosted_pilot/controller_entry.py',
    'tools/hosted_pilot/controller_handoff.py',
    'tools/hosted_pilot/controller_source.py',
    'tools/hosted_pilot/browser_render.py',
    'tools/hosted_pilot/browser_session.py',
    'tools/agent_e2e_studio_render_cli.mjs',
    'tools/agent_e2e_studio_render.mjs',
    'tools/agent_e2e_studio_render.py',
    'tools/agent_e2e_native_authoring_bindings.py',
    'tools/agent_e2e_native_custody_bindings.py',
    'tools/agent_e2e_studio_custody_observer.py',
    'certification/installed-clients/render_png.py',
    'tools/hosted_pilot/fixture_handoff.py',
    'tools/hosted_pilot/fixture_provisioner.py',
    'tools/hosted_pilot/native_bootstrap.py',
    'tools/hosted_pilot/human_memberships.py',
    'tools/hosted_pilot/setup_key_retirement.py',
    'tools/hosted_pilot/setup_key_retirement_dispatch.py',
    'tools/hosted_pilot/prepare.py',
    'tools/hosted_pilot/console_turn.py',
    'tools/agent_e2e_console_conversation.py',
    'tools/agent_e2e_backend.py',
    'tools/agent_e2e_controller.py',
    'tools/agent_e2e_campaign_budget.py',
    'tools/hosted_pilot/worker_mcp_configuration.py',
)
SOURCE_LIMIT = 256 * 1024 * 1024
BUNDLE_LIMIT = 128 * 1024 * 1024


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def portable(name):
    path = PurePosixPath(name)
    require(name and not path.is_absolute() and '..' not in path.parts and '\\' not in name
            and '.git' not in path.parts and not any(ord(char) < 32 for char in name) and str(path) == name,
            'unsafe controller archive path')
    return path


def contract(root):
    rows = {}
    for name in CONTRACT_PATHS:
        path = Path(root) / name
        require(path.is_file() and not path.is_symlink(), 'missing immutable controller runtime contract file: ' + name)
        rows[name] = digest(path.read_bytes())
    return digest(canonical(rows))


def bundle_bytes(root):
    root = Path(root)
    target = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=target, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(root.rglob('*')):
            require(not path.is_symlink(), 'controller bundles cannot contain symbolic links')
            if path.is_dir():
                continue
            require(path.is_file(), 'controller bundles contain only regular files')
            name = path.relative_to(root).as_posix()
            portable(name)
            data = path.read_bytes()
            total += len(data)
            require(total <= BUNDLE_LIMIT, 'controller bundle exceeds bounded size')
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    data = target.getvalue()
    require(len(data) <= BUNDLE_LIMIT, 'controller bundle archive exceeds bounded size')
    return data


def extract(data, destination, *, limit):
    """Never extract links, devices, duplicate paths, or files outside fresh custody."""
    require(len(data) <= limit, 'controller archive exceeds bounded size')
    root = Path(destination)
    require(not root.exists(), 'controller extraction destination must be fresh')
    rows, names, total = [], set(), 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for member in archive:
            name = member.name.rstrip('/') if member.isdir() else member.name
            portable(name)
            require(name not in names, 'duplicate controller archive path')
            names.add(name)
            require(member.isdir() or member.isfile(), 'controller archive cannot contain links or devices')
            require(member.size >= 0 and not (member.mode & 0o7000), 'unsafe controller archive mode')
            total += member.size
            require(total <= limit and len(names) <= 25000, 'controller expanded archive exceeds bounds')
            rows.append((member, None if member.isdir() else archive.extractfile(member).read()))
    root.mkdir(mode=0o700)
    for member, content in rows:
        target = root / member.name
        if member.isdir():
            target.mkdir(mode=0o755, parents=True, exist_ok=True)
        else:
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with target.open('xb') as output:
                output.write(content)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return root


def seal_source(root, tree_id):
    """Create a tracked index, prove the reviewed tree, then forbid writes."""
    root = Path(root)
    for args in [['git', 'init', '--quiet'], ['git', 'add', '--force', '.']]:
        subprocess.run(args, cwd=root, check=True, capture_output=True)
    observed = subprocess.run(['git', 'write-tree'], cwd=root, check=True, capture_output=True).stdout.decode().strip()
    require(observed == tree_id, 'downloaded controller source differs from reviewed Git tree')
    for path in sorted(root.rglob('*'), reverse=True):
        require(not path.is_symlink(), 'controller source cannot contain symbolic links')
        path.chmod(path.stat().st_mode & ~0o222)
    root.chmod(0o555)
    return observed
