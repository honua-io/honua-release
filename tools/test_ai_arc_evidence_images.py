"""Evidence-only image retention (founder request: screenshots along the way)."""
from __future__ import annotations

import hashlib
import json
import sys
import zlib
import struct
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_arc_evidence_images as E  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def png(width=2, height=2, salt=0):
    raw = b''.join(b'\x00' + bytes([salt % 256, 0, 0, 255]) * width for _ in range(height))
    chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (E.PNG + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


@pytest.fixture
def sink(tmp_path):
    E.configure(tmp_path / 'shots')
    yield tmp_path / 'shots'
    E.configure(None)


def manifest(d):
    return json.loads((d / 'manifest.json').read_text())


def test_a_kept_image_is_byte_identical_to_the_action_logs_hash(sink):
    image = png()
    name = E.retain('step', '0001-studio_click', image, expected_sha256=hashlib.sha256(image).hexdigest(),
                    url='http://127.0.0.1:8137/studio/maps/1')
    kept = sink / name
    assert kept.read_bytes() == image and (kept.stat().st_mode & 0o777) == 0o600
    row = manifest(sink)['kept'][0]
    assert row['sha256'] == row['actionLogSha256'] == 'sha256:' + hashlib.sha256(image).hexdigest()


def test_a_hash_mismatch_is_never_kept(sink):
    assert E.retain('step', 'x', png(), expected_sha256='0' * 64) is None
    assert manifest(sink)['dropped']['hash-mismatch'] == 1 and not list(sink.glob('*.png'))


@pytest.mark.parametrize('url', ['http://127.0.0.1:4180/oauth2/sign_in', 'http://x/signin?next=/', 'http://x/login',
                                 'http://x/auth/callback?code=abc', 'http://x/settings/api-keys', 'http://x/token',
                                 'unknown://sign-in'])
def test_sign_in_and_credential_pages_are_never_kept(sink, url):
    assert E.retain('step', 'x', png(), url=url) is None
    assert manifest(sink)['dropped']['credential-page'] == 1


def test_size_count_and_format_caps_hold(sink, monkeypatch):
    monkeypatch.setattr(E, 'MAX_BYTES', 200)
    assert E.retain('step', 'big', png(64, 64)) is None
    assert E.retain('step', 'text', b'not a png') is None
    monkeypatch.setattr(E, 'MAX_BYTES', 2 * 1024 * 1024)
    for i in range(E.MAX_IMAGES + 5):
        E.retain('step', f'{i}', png(salt=i))
    m = manifest(sink)
    assert len(m['kept']) == E.MAX_IMAGES == len(list(sink.glob('*.png')))
    assert m['dropped'] == {'cap': 5, 'size': 1, 'credential-page': 0, 'not-png': 1, 'hash-mismatch': 0}


def test_nothing_is_kept_when_the_sink_is_off(tmp_path):
    E.configure(None)
    assert E.retain('step', 'x', png()) is None


def test_the_browser_server_keeps_the_screenshot_the_action_log_binds(sink, tmp_path):
    from test_agent_browser_studio import FakePage, ActionLog, BLUE_PNG
    from agent_browser_studio import StudioBrowserServer, OriginLock
    page = FakePage(BLUE_PNG)
    page.url = 'http://127.0.0.1:8137/studio'
    log = ActionLog(tmp_path / 'log', 1, set(), 20)
    server = StudioBrowserServer('http://127.0.0.1:8137', log, str(tmp_path), page_factory=lambda lock: page)
    server.lock, server.page = OriginLock('http://127.0.0.1:8137'), page
    _status, _text, digest = server.perform('studio_navigate', {'url': '/studio/maps'})
    row = manifest(sink)['kept'][0]
    assert row['sha256'] == 'sha256:' + digest and (sink / row['file']).read_bytes() == BLUE_PNG
    assert row['label'] == '0001-studio_navigate'
    page.url = 'http://127.0.0.1:8137/oauth2/sign_in'      # e.g. the session expired mid-stage
    server.perform('studio_click', {'selector': '[data-test=save]'})
    assert len(manifest(sink)['kept']) == 1 and manifest(sink)['dropped']['credential-page'] == 1


def test_no_scorer_or_observer_reads_the_evidence_images():
    """Evidence only: the scorer inputs never import the sink or read its folder."""
    import ai_arc_runner as R
    scorer = [p for g in R.SCORER_GLOBS for p in (sorted(ROOT.glob(g)) if '*' in g else [ROOT / g]) if p.is_file()
              and p.suffix == '.py']
    for path in scorer + [ROOT / 'tools/ai_arc_safety.py']:
        text = path.read_text(encoding='utf-8')
        assert 'ai_arc_evidence_images' not in text and "'screenshots'" not in text, path
    source = (ROOT / 'tools/ai_arc_evidence_images.py').read_text()
    assert not any(t in source for t in ('read_bytes', 'read_text', '.read(', "'rb'", 'os.O_RDONLY')), \
        'the sink never reads files'
