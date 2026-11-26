"""Verified static Console bundle loading, custody facts, and safe extraction.

Loads the manifest-bound `honua-console` static bundle zip, verifies its sha256
digest, and extracts it safely into a private directory while rejecting path
traversal, symlinks, and missing static SPA entrypoints.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import mimetypes
import os
import posixpath
import re
import shutil
import stat
import zipfile
from pathlib import Path
from urllib.parse import unquote

MAX_BUNDLE_FILES = 5000
MAX_BUNDLE_UNCOMPRESSED = 256 * 1024 * 1024
REQUIRED_ENTRYPOINTS = ('index.html', 'config.json', '_framework/blazor.webassembly.js')
EXTRA_MIME_TYPES = {
    '.mjs': 'text/javascript; charset=utf-8',
    '.cjs': 'text/javascript; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.wasm': 'application/wasm',
    '.json': 'application/json; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.dat': 'application/octet-stream',
    '.blat': 'application/octet-stream',
}


@dataclass(frozen=True)
class BoundConsoleBundle:
    """Verified static Console bundle payload and its manifest identity."""
    bundle_bytes: bytes
    artifact_sha256: str
    source_revision: str
    artifact_version: str | None = None


def _bundle_custody_base(component: dict) -> dict:
    fact = {'artifactSha256': component.get('artifactSha256'), 'sourceRevision': component.get('sha')}
    if component.get('artifactVersion'):
        fact['artifactVersion'] = component['artifactVersion']
    return fact


def exercised_bundle_custody(component: dict) -> dict:
    """Custody fact recording that the case served the manifest-bound static Console bundle."""
    return {**_bundle_custody_base(component), 'exercised': True}


def unexercised_bundle_custody(component: dict) -> dict:
    """Custody fact recording that the case ran a fallback image instead of the bound bundle."""
    return {**_bundle_custody_base(component), 'exercised': False}


def load_bound_console_bundle(component: dict, *, github=None) -> bytes:
    """Fetch and hash-verify the static Console bundle bytes the manifest binds."""
    import static_bundle
    name = static_bundle.artifact_name(component)
    expected = component.get('artifactSha256')
    revision = component.get('artifactSourceRevision') or component.get('sha')
    repo_url = str(component.get('repository') or '')
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', repo_url)
    if not name or not match or not isinstance(expected, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', expected):
        raise ValueError('release manifest honua-console static bundle metadata is incomplete')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('release manifest honua-console static bundle source revision is invalid')
    override_path = os.environ.get('HONUA_CONSOLE_BUNDLE_PATH', '').strip()
    if override_path:
        raw = Path(override_path).read_bytes()
    else:
        if github is None:
            from resolve_trunk_candidate import GitHub
            github = GitHub()
        artifact = {
            'kind': static_bundle.KIND,
            'coordinate': static_bundle.coordinate(match.group(1), name),
            'sourceRevision': revision,
            'sha256': expected,
        }
        raw = static_bundle.published_bytes(artifact, github)
    actual = 'sha256:' + hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError(f'bound Console static bundle digest {actual} differs from manifest {expected}')
    return raw


def _safe_zip_member(info: zipfile.ZipInfo, root: Path) -> Path:
    name = info.filename
    if not name or name.startswith(('/', '\\')) or '\\' in name or '\x00' in name:
        raise ValueError(f'unsafe static bundle entry: {name!r}')
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError(f'static bundle must not contain symlinks: {name!r}')
    parts = posixpath.normpath(name).split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError(f'unsafe static bundle path: {name!r}')
    target = (root / Path(*parts)).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f'static bundle entry escapes root: {name!r}')
    return target


def _verify_extracted_bundle(root: Path) -> None:
    for required in REQUIRED_ENTRYPOINTS:
        if not (root / required).is_file():
            raise ValueError(f'Console bundle is missing required entrypoint: {required}')
    index_html = (root / 'index.html').read_text(encoding='utf-8', errors='replace')
    if '<base href="/console/"' not in index_html:
        raise ValueError('Console bundle index.html must use <base href="/console/">')


def extract_console_bundle(bundle_bytes: bytes, dest_dir: Path, *, expected_sha256: str) -> Path:
    """Verify `bundle_bytes` against `expected_sha256` and extract safely into `dest_dir`."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', expected_sha256):
        raise ValueError('invalid bound Console bundle sha256')
    if not isinstance(bundle_bytes, (bytes, bytearray)):
        raise ValueError('Console bundle bytes must be bytes')
    actual = 'sha256:' + hashlib.sha256(bundle_bytes).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f'Console bundle digest {actual} differs from bound {expected_sha256}')
    root = dest_dir.resolve()
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError('Console bundle is not a valid zip archive') from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_BUNDLE_FILES:
            raise ValueError('Console bundle exceeds entry count limit')
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            target = _safe_zip_member(info, root)
            total += info.file_size
            if total > MAX_BUNDLE_UNCOMPRESSED:
                raise ValueError('Console bundle exceeds uncompressed size limit')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, 'r') as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst)
    _verify_extracted_bundle(root)
    return root


def static_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in EXTRA_MIME_TYPES:
        return EXTRA_MIME_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or 'application/octet-stream'


def resolve_static_asset(root: Path, rel_path: str) -> Path | None:
    """Resolve a relative static asset strictly under `root`; return None on traversal or miss."""
    decoded = unquote(rel_path)
    if '\x00' in decoded or '\\' in decoded or decoded.startswith('/'):
        return None
    parts = [part for part in decoded.split('/') if part]
    if any(part in ('.', '..') for part in parts):
        return None
    candidate = (root / Path(*parts)).resolve()
    if root not in candidate.parents or not candidate.is_file():
        return None
    return candidate
