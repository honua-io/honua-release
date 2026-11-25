"""Fail-closed tests for live promise receipt emitters and evidence retrieval."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import emit_live_promise_receipts as emitter
import fetch_verified_evidence as fetcher

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
LOCK = 'sha256:' + 'c' * 64
NOW = dt.datetime(2026, 11, 24, 18, 0, tzinfo=dt.timezone.utc)
OBSERVED_AT = '2026-11-24T17:00:00Z'
NEW_EVIDENCE_WORKFLOWS = (
    'gate-launch-stack.yml',
    'gate-runs-itself.yml',
    'gate-whitehat.yml',
    'gate-rc2-aot-regression.yml',
    'gate-rc2-upgrade-recovery.yml',
    'gate-rc2-client-proxy.yml',
    'gate-rc2-tenant-security.yml',
    'gate-rc2-aot-soak.yml',
)


def _fresh_observed() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(minutes=5)
    return stamp.isoformat().replace('+00:00', 'Z')


def _identity(observed_at: str | None = None) -> dict:
    return {
        'serverSha': SHA,
        'imageDigest': DIGEST,
        'platformLockDigest': LOCK,
        'observedAt': observed_at or _fresh_observed(),
    }


def test_emit_launch_stack(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pdir = Path('proof')
    pdir.mkdir()
    (pdir / 'launch-stack-proof.json').write_text(json.dumps({
        **_identity(),
        'stackName': 'honua-gate-rc2', 'orchestrator': 'ecs', 'redis': False,
        'defaultImage': 'postgis-only', 'protectionProfile': 'Hardened',
        'publishedAotImage': True, 'imageSizeBytes': 250_000_000, 'installConsoleMinutes': 14.5,
    }))
    assert emitter.main([
        'launch-stack', '--proof-dir', 'proof', '--server-sha', SHA,
        '--server-digest', DIGEST, '--lock-digest', LOCK, '--out', 'out-ls',
    ]) == 0
    assert (tmp_path / 'out-ls/cloud-launch-stack.json').is_file()
    notes = (tmp_path / 'out-ls/release-notes.md').read_text()
    assert '<!-- honua-outcome:install-console --> 14.5 minutes' in notes


def test_emit_runs_itself(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pdir = Path('proof')
    pdir.mkdir()
    (pdir / 'runs-itself-proof.json').write_text(json.dumps({
        **_identity(),
        'stackName': 'honua-gate-rc2', 'unattendedPersonMinutes': 0,
        'rows': {row_id: {'status': 'PASS', 'recoveryMinutes': 2.0} for row_id in emitter.OPERATES_IDS},
    }))
    assert emitter.main([
        'runs-itself', '--proof-dir', 'proof', '--server-sha', SHA,
        '--server-digest', DIGEST, '--lock-digest', LOCK, '--out', 'out-ri',
    ]) == 0
    assert (tmp_path / 'out-ri/operates-12-notify.json').is_file()
    notes = (tmp_path / 'out-ri/release-notes.md').read_text()
    assert '<!-- honua-outcome:unattended-person --> 0 minutes' in notes


def test_emit_whitehat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pdir = Path('proof')
    pdir.mkdir()
    r1, r2 = b'Fable sanitized report\n', b'Astra sanitized report\n'
    (pdir / 'fable-review.md').write_bytes(r1)
    (pdir / 'astra-review.md').write_bytes(r2)
    (pdir / 'whitehat-proof.json').write_text(json.dumps({
        **_identity(),
        'stackName': 'honua-gate-whitehat-rc2',
        'stackId': 'arn:aws:cloudformation:us-east-1:123456789012:stack/honua-gate-whitehat-rc2/1',
        'openNewHighFindings': 0, 'openNewCriticalFindings': 0,
        'reviews': [
            {'model': 'claude-fable-5-2', 'reviewer': 'fable', 'source': True, 'live': True,
             'status': 'PASS', 'report': 'fable-review.md', 'reportSha256': hashlib.sha256(r1).hexdigest()},
            {'model': 'gpt-6-astra', 'reviewer': 'astra', 'source': True, 'live': True,
             'status': 'PASS', 'report': 'astra-review.md', 'reportSha256': hashlib.sha256(r2).hexdigest()},
        ],
    }))
    assert emitter.main([
        'whitehat', '--proof-dir', 'proof', '--server-sha', SHA,
        '--server-digest', DIGEST, '--lock-digest', LOCK, '--out', 'out-wh',
    ]) == 0
    assert (tmp_path / 'out-wh/security-whitehat.json').is_file()
    assert (tmp_path / 'out-wh/fable-review.md').read_bytes() == r1


def test_emit_rejects_traversal_oversized_stale_and_wrong_sha(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pdir = Path('proof')
    pdir.mkdir()
    base = {
        **_identity(),
        'stackName': 'honua-gate-rc2', 'orchestrator': 'ecs', 'redis': False,
        'defaultImage': 'postgis-only', 'protectionProfile': 'Hardened',
        'publishedAotImage': True, 'imageSizeBytes': 250_000_000, 'installConsoleMinutes': 14.5,
    }
    for bad in (
        {'imageSizeBytes': 999_000_000},
        {'observedAt': '2020-01-01T00:00:00Z'},
        {'serverSha': 'f' * 40},
    ):
        (pdir / 'launch-stack-proof.json').write_text(json.dumps({**base, **bad}))
        with pytest.raises(SystemExit):
            emitter.main([
                'launch-stack', '--proof-dir', 'proof', '--server-sha', SHA,
                '--server-digest', DIGEST, '--lock-digest', LOCK, '--out', 'out-bad',
            ])
    with pytest.raises(SystemExit):
        emitter.main([
            'launch-stack', '--proof-dir', '../proof', '--server-sha', SHA,
            '--server-digest', DIGEST, '--lock-digest', LOCK, '--out', 'out-bad',
        ])


def test_fetch_verified_evidence_accepts_trunk_run_and_rejects_untrusted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_meta = {
        'status': 'completed',
        'conclusion': 'success',
        'head_branch': 'trunk',
        'updated_at': OBSERVED_AT,
    }

    def fake_gh(cmd, capture_output=True, text=True, check=False):
        if cmd[:2] == ['gh', 'api']:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(run_meta), '')
        out_dir = Path(cmd[cmd.index('--dir') + 1])
        (out_dir / 'proof.json').write_text('{}', encoding='utf-8')
        return subprocess.CompletedProcess(cmd, 0, '', '')

    src = fetcher.EvidenceSource('honua-io/honua-release', '123456', 'rc2-proof')
    fetched = fetcher.fetch_evidence(src, Path('evidence'), runner=fake_gh, now=NOW)
    assert (fetched / 'proof.json').is_file()

    for bad_src in (
        fetcher.EvidenceSource('evil/repo', '123456', 'rc2-proof'),
        fetcher.EvidenceSource('honua-io/honua-release', 'abc', 'rc2-proof'),
        fetcher.EvidenceSource('honua-io/honua-release', '123456', '../escape'),
    ):
        with pytest.raises(ValueError):
            fetcher.validate_source(bad_src)

    for bad_meta in (
        {**run_meta, 'status': 'in_progress'},
        {**run_meta, 'conclusion': 'failure'},
        {**run_meta, 'head_branch': 'feature/untrusted'},
        {**run_meta, 'updated_at': '2020-01-01T00:00:00Z'},
    ):
        with pytest.raises(ValueError):
            fetcher.verify_run_metadata(bad_meta, NOW)


def test_fetch_verified_evidence_rejects_symlinks_and_empty_dirs(tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises(ValueError):
        fetcher.verify_downloaded_tree(empty)

    sym_dir = tmp_path / 'sym'
    sym_dir.mkdir()
    target = tmp_path / 'outside.json'
    target.write_text('{}', encoding='utf-8')
    (sym_dir / 'link.json').symlink_to(target)
    with pytest.raises(ValueError):
        fetcher.verify_downloaded_tree(sym_dir)


def test_new_gate_workflows_fetch_authenticated_evidence_and_run_stdlib_only():
    for name in NEW_EVIDENCE_WORKFLOWS:
        text = (ROOT / '.github/workflows' / name).read_text(encoding='utf-8')
        assert 'actions: read' in text, name
        assert 'tools/fetch_verified_evidence.py' in text, name
        assert '--out evidence' in text, name

    check = subprocess.run(
        [sys.executable, '-S', '-c',
         "import sys; sys.path.insert(0, 'tools'); "
         "import fetch_verified_evidence, rc2_rigor, emit_live_promise_receipts"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert check.returncode == 0, check.stderr
