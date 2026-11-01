"""Evidence-only image retention for AI-arc stages (founder request 2026-11-01: screenshots along the way).

The runner points the sink at a stage's private evidence folder; the Studio browser server and the
independent render observers hand it the PNGs they already produce (controller screenshots, whose sha256 is
in the action log, and the controller-fetched server renders the observers measure). Files are written,
never read back: no scorer or observer reads this folder, and nothing here reaches the model.

Rules: PNG bytes only; at most MAX_BYTES per image and MAX_IMAGES per stage (the rest are counted, not kept);
a page on a sign-in, OAuth, login or credential URL is never kept; every kept file's sha256 is recorded in
manifest.json beside the hash the caller states (the action log's screenshotSha256) and must equal it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
MAX_IMAGES = 80
PNG = b'\x89PNG\r\n\x1a\n'
CREDENTIAL_PAGE = re.compile(r'(?:^|[/?#&=._-])(?:oauth2?|sign[-_]?in|sign[-_]?on|login|log[-_]?in|logout|auth|'
                             r'authorize|callback|token|password|api[-_]?keys?|credentials?|secrets?)(?:$|[/?#&=._-])',
                             re.I)

_lock = threading.Lock()
_state = {'dir': None, 'kept': [], 'dropped': {'cap': 0, 'size': 0, 'credential-page': 0, 'not-png': 0,
                                                'hash-mismatch': 0}}


def configure(directory) -> None:
    """Start retaining into ``directory`` (created mode 0700), or stop with None."""
    with _lock:
        _state['dir'] = None if directory is None else Path(directory)
        _state['kept'] = []
        _state['dropped'] = {k: 0 for k in _state['dropped']}
        if _state['dir'] is not None:
            _state['dir'].mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(_state['dir'], 0o700)


def credential_page(url) -> bool:
    return isinstance(url, str) and bool(CREDENTIAL_PAGE.search(url))


def retain(kind: str, label: str, png: bytes, *, expected_sha256: str | None = None, url: str | None = None):
    """Keep one image if the sink is on and every rule holds; return the file name kept, else None."""
    with _lock:
        directory = _state['dir']
        if directory is None or not isinstance(png, (bytes, bytearray)) or not png:
            return None
        reason = None
        digest = hashlib.sha256(png).hexdigest()
        if not bytes(png[:8]) == PNG:
            reason = 'not-png'
        elif credential_page(url):
            reason = 'credential-page'
        elif len(png) > MAX_BYTES:
            reason = 'size'
        elif expected_sha256 is not None and expected_sha256 != digest:
            reason = 'hash-mismatch'
        elif len(_state['kept']) >= MAX_IMAGES:
            reason = 'cap'
        if reason:
            _state['dropped'][reason] += 1
            _write_manifest(directory)
            return None
        index = len(_state['kept']) + 1
        safe = re.sub(r'[^A-Za-z0-9_.-]+', '-', f'{kind}-{label}')[:80].strip('-') or kind
        name = f'{index:03d}-{safe}.png'
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(bytes(png))
        _state['kept'].append({'file': name, 'kind': kind, 'label': label[:120], 'bytes': len(png),
                               'sha256': 'sha256:' + digest,
                               **({'actionLogSha256': 'sha256:' + expected_sha256} if expected_sha256 else {})})
        _write_manifest(directory)
        return name


def _write_manifest(directory: Path) -> None:
    raw = json.dumps({'schema': 'honua.ai-arc.evidence-images/v1', 'maxBytes': MAX_BYTES, 'maxImages': MAX_IMAGES,
                      'kept': _state['kept'], 'dropped': _state['dropped'],
                      'rule': 'evidence only; never read by the scorer; credential pages are never kept'},
                     sort_keys=True, indent=1).encode()
    tmp = directory / 'manifest.json.tmp'
    tmp.write_bytes(raw)
    os.chmod(tmp, 0o600)
    tmp.replace(directory / 'manifest.json')
