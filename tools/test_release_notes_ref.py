import hashlib
import subprocess

import pytest

from release_notes_ref import snapshot
from test_mint_nightly_lock import inputs
from test_platform_lock_bundle import candidate


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True, capture_output=True).stdout


def test_generated_notes_name_exact_bytes_at_reachable_revision(inputs, tmp_path):
    report, paths = inputs
    output = tmp_path / 'notes'
    revision, refs = snapshot(*paths, report, output, {'attestations/package.json': b'{"bundle":true}'})
    checkout = tmp_path / 'reader'
    checkout.mkdir()
    git(checkout, 'init', '-q')
    git(checkout, 'fetch', str(output / 'release-notes.bundle'), 'HEAD')
    assert git(checkout, 'rev-parse', 'FETCH_HEAD').decode().strip() == revision
    for path, ref in refs.items():
        raw = git(checkout, 'show', f'{revision}:{path}')
        assert ref == (f'https://github.com/honua-io/honua-release@{revision}:{path}'
                       '#sha256:' + hashlib.sha256(raw).hexdigest())
    notes = git(checkout, 'show', f'{revision}:release-notes/2026.1-rc.3.md').decode()
    assert 'Breaking changes' in notes and report['candidate']['train']['runUrl'] in notes
    assert git(checkout, 'show', '-s', '--format=%an <%ae>|%cn <%ce>', revision).decode().strip() == \
        'Mike McDougall <mike@honua.io>|Mike McDougall <mike@honua.io>'
    assert (output / 'release-notes-ref.txt').read_text().strip() == refs['release-notes/2026.1-rc.3.md']


def test_snapshot_is_deterministic_and_uses_candidate_bytes(inputs, tmp_path):
    report, paths = inputs
    first = snapshot(*paths, report, tmp_path / 'one')
    second = snapshot(*paths, report, tmp_path / 'two')
    assert first == second


def test_notes_refuses_a_red_or_different_candidate(inputs, tmp_path):
    report, paths = inputs
    report['gates'][0]['status'] = 'fail'
    with pytest.raises(ValueError, match='all-green live report'):
        snapshot(*paths, report, tmp_path / 'out')
    report['gates'][0]['status'] = 'pass'
    paths[0].write_text(paths[0].read_text() + '# different bytes\n')
    with pytest.raises(ValueError, match='not bound'):
        snapshot(*paths, report, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'a/../outside', 'a\\outside'])
def test_snapshot_refuses_escaped_document_paths(inputs, tmp_path, path):
    report, paths = inputs
    with pytest.raises(ValueError, match='relative repository file path'):
        snapshot(*paths, report, tmp_path / 'out', {path: b'no'})
