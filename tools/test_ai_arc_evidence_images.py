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


@pytest.fixture(autouse=True)
def fresh_process():
    E._reset_for_tests()          # each test stands in for one trial process
    yield
    E._reset_for_tests()


@pytest.fixture
def sink(tmp_path):
    handle = E.open_sink(tmp_path / 'shots', owner='run-a')
    yield handle
    E.close_sink(handle)


def manifest(s):
    return json.loads((s.directory / 'manifest.json').read_text())


def test_a_kept_image_is_byte_identical_to_the_action_logs_hash(sink):
    image = png()
    name = sink.retain('step', '0001-studio_click', image, expected_sha256=hashlib.sha256(image).hexdigest(),
                       url='http://127.0.0.1:8137/studio/maps/1', check_url=True)
    kept = sink.directory / name
    assert kept.read_bytes() == image and (kept.stat().st_mode & 0o777) == 0o600
    row = manifest(sink)['kept'][0]
    assert row['sha256'] == row['actionLogSha256'] == 'sha256:' + hashlib.sha256(image).hexdigest()


def test_a_hash_mismatch_is_never_kept(sink):
    assert sink.retain('step', 'x', png(), expected_sha256='0' * 64) is None
    assert manifest(sink)['dropped']['hash-mismatch'] == 1 and not list(sink.directory.glob('*.png'))


@pytest.mark.parametrize('url', ['http://127.0.0.1:4180/oauth2/sign_in', 'http://x/signin?next=/', 'http://x/login',
                                 'http://x/auth/callback?code=abc', 'http://x/settings/api-keys', 'http://x/token',
                                 None, ''])
def test_sign_in_and_credential_pages_are_never_kept(sink, url):
    assert sink.retain('step', 'x', png(), url=url, check_url=True) is None
    assert manifest(sink)['dropped']['credential-page'] == 1


def test_size_count_and_format_caps_hold(sink, monkeypatch):
    monkeypatch.setattr(E, 'MAX_BYTES', 200)
    assert sink.retain('step', 'big', png(64, 64)) is None
    assert sink.retain('step', 'text', b'not a png') is None
    monkeypatch.setattr(E, 'MAX_BYTES', 2 * 1024 * 1024)
    for i in range(E.MAX_IMAGES + 5):
        sink.retain('render', f'{i}', png(salt=i))
    m = manifest(sink)
    assert len(m['kept']) == E.MAX_IMAGES == len(list(sink.directory.glob('*.png')))
    assert {k: v for k, v in m['dropped'].items() if v} == {'cap': 5, 'size': 1, 'not-png': 1}


def test_nothing_is_current_when_no_stage_has_a_sink():
    assert E.current() is None


def test_a_late_writer_from_a_closed_sink_never_reaches_the_next_runs_evidence(tmp_path):
    """Sol 02c55045 item 1 / d9aa66b5 item 1: one sink per (trial) process. A second open, for another run,
    gets no sink; a late producer from run A, whenever it is constructed, finds A's closed sink or none."""
    a = E.open_sink(tmp_path / 'a', owner='run-a')
    captured_early = E.current()
    E.close_sink(a)
    b = E.open_sink(tmp_path / 'b', owner='run-b')
    assert b is None and E.current() is None, 'a second run in the same process gets no sink'
    captured_late = E.current()                         # a delayed run-A producer constructed now
    assert captured_late is None
    assert captured_early.retain('step', 'from-a', png()) is None and a.dropped['closed'] == 1
    assert not (tmp_path / 'b').exists()


def test_a_browser_server_constructed_late_keeps_nothing(tmp_path):
    """Sol d9aa66b5 item 1: delayed producer construction after its run's sink closed."""
    from test_agent_browser_studio import FakePage, ActionLog, BLUE_PNG
    from agent_browser_studio import StudioBrowserServer, OriginLock
    a = E.open_sink(tmp_path / 'a', owner='run-a')
    E.close_sink(a)
    E.open_sink(tmp_path / 'b', owner='run-b')
    page = FakePage(BLUE_PNG)
    page.url = 'http://127.0.0.1:8137/studio'
    log = ActionLog(tmp_path / 'log', 1, set(), 20)
    server = StudioBrowserServer('http://127.0.0.1:8137', log, str(tmp_path), page_factory=lambda lock: page)
    server.lock, server.page = OriginLock('http://127.0.0.1:8137'), page
    server.perform('studio_navigate', {'url': '/studio/maps'})
    assert not list((tmp_path / 'a').glob('*.png')) and not (tmp_path / 'b').exists()


def test_sink_initialization_failure_leaves_retention_off(tmp_path, monkeypatch):
    """Sol d9aa66b5 item 2: never raises; the stage continues without retention."""
    monkeypatch.setattr(E.Sink, '_manifest', lambda self: (_ for _ in ()).throw(OSError('disk full')))
    assert E.open_sink(tmp_path / 'shots', owner='run-a') is None and E.current() is None


def test_retention_io_failure_never_raises(sink, monkeypatch):
    """Sol 02c55045 item 3: a manifest write failure is counted, not raised."""
    monkeypatch.setattr(E.Sink, '_manifest', lambda self: (_ for _ in ()).throw(OSError('disk full')))
    assert sink.retain('render', 'x', png()) is None
    assert sink.dropped['io-error'] == 1


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
    assert row['sha256'] == row['actionLogSha256'] == 'sha256:' + digest
    assert (sink.directory / row['file']).read_bytes() == BLUE_PNG and log.rows[0]['status'] == 'pass'
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



def test_a_retention_failure_leaves_the_action_outcome_and_digest_intact(sink, tmp_path, monkeypatch):
    """Sol 02c55045 item 3: evidence I/O can never change the action's status or its sealed digest."""
    from test_agent_browser_studio import FakePage, ActionLog, BLUE_PNG
    from agent_browser_studio import StudioBrowserServer, OriginLock
    monkeypatch.setattr(E.Sink, '_manifest', lambda self: (_ for _ in ()).throw(OSError('disk full')))
    page = FakePage(BLUE_PNG)
    page.url = 'http://127.0.0.1:8137/studio'
    log = ActionLog(tmp_path / 'log', 1, set(), 20)
    server = StudioBrowserServer('http://127.0.0.1:8137', log, str(tmp_path), page_factory=lambda lock: page)
    server.lock, server.page = OriginLock('http://127.0.0.1:8137'), page
    status, _text, digest = server.perform('studio_navigate', {'url': '/studio/maps'})
    assert status == 'ok' and digest == hashlib.sha256(BLUE_PNG).hexdigest() and log.rows[0]['status'] == 'pass'



def test_a_partial_write_leaves_nothing_and_is_never_exported(sink, tmp_path, monkeypatch):
    """Sol fa22710e: a write that fails after a prefix never leaves a PNG behind or reaches the export."""
    import os as _os
    import ai_arc_runner as R
    real = _os.fsync
    monkeypatch.setattr(E.os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('disk full')))
    for i in range(E.MAX_IMAGES + 1):
        assert sink.retain('render', f'{i}', png(salt=i)) is None
    assert not list(sink.directory.glob('*.png')) and not list(sink.directory.glob('*.part'))
    assert sink.dropped['io-error'] == E.MAX_IMAGES + 1
    monkeypatch.setattr(E.os, 'fsync', real)
    good = png(salt=200)
    sink.retain('render', 'good', good)
    (sink.directory / 'stray.png').write_bytes(png(salt=201))           # never committed by the sink
    (sink.directory / '099-stray.png.part').write_bytes(b'\x89PNG partial')
    out = tmp_path / 'out'
    R.export_private(sink.directory.parent, out, [])
    exported = sorted(p.name for p in (out / sink.directory.name).iterdir())
    assert exported == ['001-render-good.png', 'manifest.json']
    assert (out / sink.directory.name / '001-render-good.png').read_bytes() == good
