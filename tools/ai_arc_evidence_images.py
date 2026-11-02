"""Evidence-only image retention for AI-arc stages (founder request 2026-11-01: screenshots along the way).

The runner opens one ``Sink`` per Studio stage on the stage's private evidence folder and closes it when the
stage ends (an outer ``finally``). Producers capture the sink that is open when they start (``current()``):
the Studio browser server at construction, the render observers at the start of each observation. A sink
that has closed rejects every later write, so a late worker from one run can never write into another run's
evidence, and opening a new sink never redirects an older producer.

Files are written, never read back: no scorer or observer reads this folder and nothing here reaches the
model. Rules: PNG bytes only; at most MAX_BYTES per image and MAX_IMAGES per sink (the rest are counted, not
kept); a page on a sign-in, OAuth, login or credential URL (or an unknown URL) is never kept; a screenshot is
kept only after the action log sealed its result with the same sha256. Retention never raises: an I/O
failure is counted and the evidence is dropped, so it cannot change an action's outcome.
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
DROP_REASONS = ('cap', 'size', 'credential-page', 'not-png', 'hash-mismatch', 'closed', 'io-error')

_lock = threading.Lock()
_current = {'sink': None, 'opened': None, 'refused': 0}


def credential_page(url) -> bool:
    return not isinstance(url, str) or not url or bool(CREDENTIAL_PAGE.search(url))


class Sink:
    """One stage's image evidence folder. Thread-safe; rejects writes once closed."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.closed = False
        self.kept = []
        self.dropped = {k: 0 for k in DROP_REASONS}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self._manifest()

    def close(self):
        with self.lock:
            self.closed = True

    def retain(self, kind: str, label: str, png, *, expected_sha256: str | None = None, url: str | None = None,
               check_url: bool = False):
        """Keep one image if the sink is open and every rule holds; return the kept file name, else None.
        Never raises."""
        try:
            with self.lock:
                if self.closed:
                    self.dropped['closed'] += 1
                    return None
                if not isinstance(png, (bytes, bytearray)) or not png:
                    return None
                reason = None
                digest = hashlib.sha256(png).hexdigest()
                if bytes(png[:8]) != PNG:
                    reason = 'not-png'
                elif check_url and credential_page(url):
                    reason = 'credential-page'
                elif len(png) > MAX_BYTES:
                    reason = 'size'
                elif expected_sha256 is not None and expected_sha256 != digest:
                    reason = 'hash-mismatch'
                elif len(self.kept) >= MAX_IMAGES:
                    reason = 'cap'
                if reason:
                    self.dropped[reason] += 1
                    self._manifest()
                    return None
                index = len(self.kept) + 1
                safe = re.sub(r'[^A-Za-z0-9_.-]+', '-', f'{kind}-{label}')[:80].strip('-') or kind
                name = f'{index:03d}-{safe}.png'
                # Written to a private part file and published by rename only when complete; a failed write
                # leaves nothing behind (Sol fa22710e).
                part = self.directory / (name + '.part')
                try:
                    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(bytes(png))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(part, self.directory / name)
                except BaseException:
                    try:
                        part.unlink()
                    except OSError:
                        pass
                    raise
                self.kept.append({'file': name, 'kind': kind, 'label': str(label)[:120], 'bytes': len(png),
                                  'sha256': 'sha256:' + digest,
                                  **({'actionLogSha256': 'sha256:' + expected_sha256} if expected_sha256 else {})})
                self._manifest()
                return name
        except Exception:  # noqa: BLE001  (evidence is optional; never change the caller's outcome)
            try:
                with self.lock:
                    self.dropped['io-error'] += 1
            except Exception:  # noqa: BLE001
                pass
            return None

    def _manifest(self):
        raw = json.dumps({'schema': 'honua.ai-arc.evidence-images/v1', 'owner': getattr(self, 'owner', None),
                          'maxBytes': MAX_BYTES, 'maxImages': MAX_IMAGES,
                          'kept': self.kept, 'dropped': self.dropped,
                          'rule': 'evidence only; never read by the scorer; credential pages are never kept'},
                         sort_keys=True, indent=1).encode()
        tmp = self.directory / 'manifest.json.tmp'
        tmp.write_bytes(raw)
        os.chmod(tmp, 0o600)
        tmp.replace(self.directory / 'manifest.json')


def open_sink(directory, owner: str = '') -> Sink | None:
    """Open the one evidence sink this process may have (a trial runs in its own process: ai_arc_campaign
    starts one per run), owned by ``owner`` (the run id). Any later open in the same process, for another run
    or stage, gets no sink and retention stays off, so a late producer from an earlier run can only ever find
    that run's (closed) sink or none (Sol d9aa66b5). A sink that cannot be initialized also leaves retention
    off: evidence is optional and never changes a stage's outcome. Never raises."""
    with _lock:
        if _current['opened'] is not None:
            _current['refused'] += 1
            return None
        _current['opened'] = str(owner)[:120] or 'unnamed'
    try:
        sink = Sink(directory)
        sink.owner = _current['opened']
    except Exception:  # noqa: BLE001
        return None
    with _lock:
        _current['sink'] = sink
    return sink


def _reset_for_tests() -> None:
    """Tests only: forget that this process opened a sink."""
    with _lock:
        if _current['sink'] is not None:
            _current['sink'].close()
        _current.update(sink=None, opened=None, refused=0)


def close_sink(sink) -> None:
    """Close ``sink``; it stops being current only if it still is (a newer stage's sink is never touched)."""
    if sink is None:
        return
    sink.close()
    with _lock:
        if _current['sink'] is sink:
            _current['sink'] = None


def current():
    """The sink open now, for a producer to capture when it starts (None when retention is off)."""
    with _lock:
        sink = _current['sink']
    return sink if sink is not None and not sink.closed else None
