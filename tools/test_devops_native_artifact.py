"""Finite archive/registry source controls, not published-install qualification."""
import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
import pytest
import devops_native_artifact as native


def expected(data=b'fixture'):
    return {'repository': native.REPOSITORY, 'tag': 'v2026.1.1', 'sourceSha': 'a' * 40,
            'rid': 'linux-x64', 'archiveSha256': hashlib.sha256(data).hexdigest(), 'releaseId': 11, 'assetId': 22}


def publication(identity):
    return {'id': 11, 'tag_name': identity['tag'], 'draft': False, 'published_at': '2026-10-20T00:00:00Z',
            'assets': [{'id': 22, 'name': native.archive_name(identity), 'state': 'uploaded', 'size': 20,
                        'digest': 'sha256:' + identity['archiveSha256']},
                       {'id': 23, 'name': native.archive_name(identity) + '.sha256', 'state': 'uploaded', 'size': 100}]}


def test_exact_registry_shape_is_a_structural_positive_only():
    identity = expected()
    assert native.verify_publication(identity, publication(identity), identity['sourceSha'])[0]['id'] == 22


@pytest.mark.parametrize('field,value', [('repository', 'foreign/repo'), ('tag', 'trunk'), ('sourceSha', 'mutable'),
                                         ('rid', []), ('rid', 'linux-arm64'), ('archiveSha256', 'abc'),
                                         ('releaseId', True), ('assetId', 0)])
def test_identity_refuses_unapproved_shapes(field, value):
    identity = expected(); identity[field] = value
    with pytest.raises(native.Refusal): native.expected_identity(identity)


@pytest.mark.parametrize('mutation', ['draft', 'unpublished', 'wrongsource', 'wrongrelease', 'wrongasset',
                                       'duplicated', 'wrongdigest', 'missingchecksum', 'boolid'])
def test_registry_mismatch_or_partial_never_becomes_published_identity(mutation):
    identity = expected(); value = publication(identity); source = identity['sourceSha']
    if mutation == 'draft': value['draft'] = True
    if mutation == 'unpublished': value['published_at'] = None
    if mutation == 'wrongsource': source = 'b' * 40
    if mutation == 'wrongrelease': value['id'] = 33
    if mutation == 'wrongasset': value['assets'][0]['id'] = 55
    if mutation == 'duplicated': value['assets'].append(copy.deepcopy(value['assets'][0]))
    if mutation == 'wrongdigest': value['assets'][0]['digest'] = 'sha256:' + 'b' * 64
    if mutation == 'missingchecksum': value['assets'].pop()
    if mutation == 'boolid': value['id'] = True
    with pytest.raises(native.Refusal): native.verify_publication(identity, value, source)


def tar(path, rows):
    with tarfile.open(path, 'w:gz') as stream:
        for name, data, kind in rows:
            member = tarfile.TarInfo(name); member.type = kind
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            member.linkname = '/outside'
            stream.addfile(member, io.BytesIO(data) if member.isfile() else None)


def elf():
    data = bytearray(64); data[:6] = b'\x7fELF\x02\x01'; struct.pack_into('<H', data, 18, 62)
    return bytes(data)


def test_archive_closure_is_exact_finite_bytes_not_execution(tmp_path):
    archive = tmp_path / 'input.tar.gz'
    tar(archive, [('./Honua.DevOps.Agent', elf(), tarfile.REGTYPE), ('./data/item', b'closed bytes', tarfile.REGTYPE)])
    identity = expected(archive.read_bytes())
    native.verify_archive(identity, archive, (identity['archiveSha256'] + '  ' + native.archive_name(identity) + '\n').encode())
    members = native.extract_sealed(identity, archive, tmp_path / 'stage')
    assert [x['path'] for x in members] == ['Honua.DevOps.Agent', 'data/item']
    assert (tmp_path / 'stage/Honua.DevOps.Agent').stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('name,kind', [('../outside', tarfile.REGTYPE), ('/outside', tarfile.REGTYPE),
                                      ('link', tarfile.SYMTYPE), ('hard', tarfile.LNKTYPE),
                                      ('pipe', tarfile.FIFOTYPE), ('x\\y', tarfile.REGTYPE)])
def test_unsafe_archive_is_refused_before_any_executable(tmp_path, name, kind):
    archive = tmp_path / 'input.tar.gz'
    tar(archive, [('Honua.DevOps.Agent', elf(), tarfile.REGTYPE), (name, b'bad', kind)])
    with pytest.raises(native.Refusal): native.extract_sealed(expected(), archive, tmp_path / 'stage')
    assert not (tmp_path / 'outside').exists()


def test_duplicate_member_and_wrong_rid_are_refused(tmp_path):
    archive = tmp_path / 'input.tar.gz'
    tar(archive, [('Honua.DevOps.Agent', elf(), tarfile.REGTYPE)] * 2)
    with pytest.raises(native.Refusal, match='duplicate'): native.extract_sealed(expected(), archive, tmp_path / 'duplicate')
    tar(archive, [('Honua.DevOps.Agent', b'#!/bin/sh\nexit 0\n', tarfile.REGTYPE)])
    with pytest.raises(native.Refusal, match='elf-rid'): native.extract_sealed(expected(), archive, tmp_path / 'script')


def test_changed_archive_or_checksum_refuses(tmp_path):
    archive = tmp_path / 'input'; archive.write_bytes(b'actual')
    identity = expected(b'actual')
    with pytest.raises(native.Refusal, match='checksum'): native.verify_archive(identity, archive, b'wrong')
    archive.write_bytes(b'changed')
    with pytest.raises(native.Refusal, match='integrity'): native.verify_archive(identity, archive, b'wrong')


def test_provenance_failure_retains_originals_and_never_stages_executable(tmp_path, monkeypatch):
    import subprocess
    archive = tmp_path / 'archive.tar.gz'
    tar(archive, [('Honua.DevOps.Agent', elf(), tarfile.REGTYPE)])
    identity = expected(archive.read_bytes()); release = publication(identity)
    release['assets'][0]['size'] = archive.stat().st_size
    checksum = (identity['archiveSha256'] + '  ' + native.archive_name(identity) + '\n').encode()
    release['assets'][1]['size'] = len(checksum)
    monkeypatch.setattr(native, 'gh_json', lambda *args: release)
    monkeypatch.setattr(native, 'resolve_tag', lambda *args: identity['sourceSha'])
    def download(asset, target, bound):
        target.write_bytes(archive.read_bytes() if asset['id'] == identity['assetId'] else checksum)
    monkeypatch.setattr(native, 'download', download)
    commands = []
    def refuse(command, stdout, stderr, timeout):
        commands.append(command)
        stdout.write_bytes(b'original-verifier-output'); stderr.write_bytes(b'original-verifier-error')
        return subprocess.CompletedProcess(command, 1, b'original-verifier-output', b'original-verifier-error')
    monkeypatch.setattr(native, 'retained_run', refuse)
    output = tmp_path / 'output'; output.mkdir()
    with pytest.raises(native.Refusal, match='provenance-unverified'): native.stage(identity, output)
    request = next(output.iterdir())
    assert not (request / 'installed').exists()
    assert (request / 'provenance.json').read_bytes() == b'original-verifier-output'
    assert (request / 'provenance.stderr').read_bytes() == b'original-verifier-error'
    assert json.loads((request / 'failure.json').read_text())['candidateQualified'] is False
    assert commands == [['gh', 'attestation', 'verify', str(request / native.archive_name(identity)),
                         '--hostname', 'github.com', '--repo', native.REPOSITORY, '--signer-workflow', native.REPOSITORY + '/' + native.WORKFLOW,
                         '--source-ref', 'refs/tags/' + identity['tag'], '--source-digest', identity['sourceSha'],
                         '--deny-self-hosted-runners', '--format', 'json']]


def test_timeout_retains_original_read_process_streams(tmp_path):
    import sys
    command = [sys.executable, '-c', "import sys,time; print('original-partial-out',flush=True); print('original-partial-error',file=sys.stderr,flush=True); time.sleep(60)"]
    with pytest.raises(native.Refusal, match='read-timeout'):
        native.retained_run(command, tmp_path / 'out', tmp_path / 'err', .3)
    assert (tmp_path / 'out').read_bytes() == b'original-partial-out\n'
    assert (tmp_path / 'err').read_bytes() == b'original-partial-error\n'


@pytest.mark.parametrize('channel', ['stdout', 'stderr'])
def test_stream_bounds_stop_owned_read_child_and_bound_actual_files(tmp_path, channel):
    import sys
    command = [sys.executable, '-c', f"import sys,time; print('before-limit',file=sys.{'stderr' if channel == 'stdout' else 'stdout'},flush=True); sys.{channel}.buffer.write(b'x'*(4*1024*1024)); sys.{channel}.flush(); time.sleep(60)"]
    with pytest.raises(native.Refusal, match=channel + '-limit'):
        native.retained_run(command, tmp_path / 'out', tmp_path / 'err', 3,
                            stdout_bound=65536, stderr_bound=65536)
    assert (tmp_path / ('out' if channel == 'stdout' else 'err')).stat().st_size == 65536
    assert (tmp_path / ('err' if channel == 'stdout' else 'out')).read_bytes() == b'before-limit\n'


def test_native_stdio_source_routing_controls():
    import subprocess
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', '--test', 'tools/devops_native_mcp.test.mjs'], cwd=root,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_tar_canonical_root_directory_is_not_an_extra_file(tmp_path):
    archive = tmp_path / 'input.tar.gz'
    tar(archive, [('.', b'', tarfile.DIRTYPE), ('./Honua.DevOps.Agent', elf(), tarfile.REGTYPE)])
    rows = native.extract_sealed(expected(), archive, tmp_path / 'stage')
    assert [row['path'] for row in rows] == ['Honua.DevOps.Agent']


@pytest.mark.parametrize('name', ['C:/outside', 'drive:relative', 'folder./file', 'folder /file', 'file.', 'file '])
def test_portable_archive_normalization_escapes_are_refused(tmp_path, name):
    archive = tmp_path / 'input.tar.gz'
    tar(archive, [('Honua.DevOps.Agent', elf(), tarfile.REGTYPE), (name, b'bad', tarfile.REGTYPE)])
    with pytest.raises(native.Refusal, match='member-path'): native.extract_sealed(expected(), archive, tmp_path / 'stage')


def test_repeated_root_entries_count_toward_bound(tmp_path, monkeypatch):
    archive = tmp_path / 'input.tar.gz'
    tar(archive, [('.', b'', tarfile.DIRTYPE)] * 3 + [('Honua.DevOps.Agent', elf(), tarfile.REGTYPE)])
    monkeypatch.setattr(native, 'MAX_MEMBERS', 3)
    with pytest.raises(native.Refusal, match='member-count'): native.extract_sealed(expected(), archive, tmp_path / 'stage')


def test_nonzero_directory_payload_is_refused(tmp_path):
    archive = tmp_path / 'input.tar.gz'
    with tarfile.open(archive, 'w:gz') as stream:
        member = tarfile.TarInfo('.'); member.type = tarfile.DIRTYPE; member.size = 1
        stream.addfile(member, io.BytesIO(b'x'))
    with pytest.raises(native.Refusal, match='member-size'): native.extract_sealed(expected(), archive, tmp_path / 'stage')


def sdk_fixture(tmp_path):
    import base64
    metadata = json.dumps({'name': '@modelcontextprotocol/sdk', 'version': '1.32.1'}).encode()
    archive = tmp_path / 'sdk.tgz'
    tar(archive, [('package/package.json', metadata, tarfile.REGTYPE),
                  ('package/dist/esm/client/index.js', b'export const source = "original";\n', tarfile.REGTYPE)])
    installed = tmp_path / 'installed'; (installed / 'dist/esm/client').mkdir(parents=True)
    (installed / 'package.json').write_bytes(metadata)
    (installed / 'dist/esm/client/index.js').write_bytes(b'export const source = "original";\n')
    integrity = 'sha512-' + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
    return installed, archive, integrity


def test_sdk_finite_archive_and_installed_closure_join(tmp_path):
    installed, archive, integrity = sdk_fixture(tmp_path)
    value = native.verify_sdk_bytes(installed, archive, integrity)
    assert value['fileCount'] == 2 and value['version'] == '1.32.1'


@pytest.mark.parametrize('mutation', ['same-version-js', 'extra-js', 'missing-js', 'symlink', 'archive-integrity'])
def test_same_version_sdk_bytes_cannot_bypass_official_archive_join(tmp_path, mutation):
    installed, archive, integrity = sdk_fixture(tmp_path)
    module = installed / 'dist/esm/client/index.js'
    if mutation == 'same-version-js': module.write_text('export const source = "tampered";')
    if mutation == 'extra-js': (installed / 'extra.js').write_text('tampered')
    if mutation == 'missing-js': module.unlink()
    if mutation == 'symlink': module.unlink(); module.symlink_to(installed / 'package.json')
    if mutation == 'archive-integrity': integrity = 'sha512-' + 'A' * 88
    assert json.loads((installed / 'package.json').read_text())['version'] == '1.32.1'
    with pytest.raises(native.Refusal): native.verify_sdk_bytes(installed, archive, integrity)


def test_binary_asset_download_pins_host_and_allows_original_binary_bytes_only(tmp_path, monkeypatch):
    import subprocess
    commands = []
    def read(command, stdout, stderr, timeout, **options):
        commands.append((command, options))
        stdout.write_bytes(b'\x7fELF\x1b\x00'); stderr.write_bytes(b'')
        return subprocess.CompletedProcess(command, 0, b'', b'')
    monkeypatch.setattr(native, 'retained_run', read)
    native.download({'id': 22, 'size': 6}, tmp_path / 'binary', 100)
    assert (tmp_path / 'binary').read_bytes() == b'\x7fELF\x1b\x00'
    assert commands == [(['gh', 'api', '--hostname', 'github.com',
                         'repos/honua-io/honua-devops/releases/assets/22',
                         '-H', 'Accept: application/octet-stream', '--allow-escape-sequences'],
                        {'stdout_bound': 6, 'capture_stdout': False})]


def test_metadata_api_pins_host_without_binary_escape_override(tmp_path, monkeypatch):
    import subprocess
    commands = []
    def read(command, stdout, stderr, timeout):
        commands.append(command); stdout.write_bytes(b'{"id":11}'); stderr.write_bytes(b'')
        return subprocess.CompletedProcess(command, 0, b'{"id":11}', b'')
    monkeypatch.setattr(native, 'retained_run', read)
    assert native.gh_json('repos/honua-io/honua-devops/releases/11', tmp_path / 'metadata') == {'id': 11}
    assert commands == [['gh', 'api', '--hostname', 'github.com', 'repos/honua-io/honua-devops/releases/11']]


def test_actual_gh_cli_binary_pipe_and_text_escape_opt_in(tmp_path):
    """Only loopback fixture bytes; no real token, remote asset or native execution."""
    import http.server
    import os
    import shutil
    import subprocess
    import threading
    cli = shutil.which('gh')
    assert cli, 'existing GitHub CLI required for binary-pipe source control'
    payload = b'\x7fELF\x1b[31m\x00original-binary'
    textual = b'original text \x1b[31mescape\n'
    observed = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(self.path)
            body = textual if self.path == '/text' else payload
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    config = tmp_path / 'gh-config'; config.mkdir(mode=0o700)
    environment = {'PATH': os.defpath, 'HOME': str(tmp_path), 'GH_CONFIG_DIR': str(config),
                   'GH_TOKEN': 'source-control-fake-key-not-a-credential', 'GH_PROMPT_DISABLED': '1',
                   'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'}
    with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            command = [cli, 'api', '--hostname', 'github.com',
                       f'http://127.0.0.1:{server.server_port}/binary', '-H', 'Accept: application/octet-stream']
            refused = subprocess.run(command, env=environment, capture_output=True, timeout=15)
            allowed = subprocess.run(command + ['--allow-escape-sequences'], env=environment,
                                     capture_output=True, timeout=15)
            assert refused.returncode == 0, refused.stderr
            assert refused.stdout == payload
            assert allowed.returncode == 0, allowed.stderr
            assert allowed.stdout == payload
            text_command = [part.replace('/binary', '/text') for part in command]
            text_refused = subprocess.run(text_command, env=environment, capture_output=True, timeout=15)
            text_allowed = subprocess.run(text_command + ['--allow-escape-sequences'], env=environment,
                                          capture_output=True, timeout=15)
            assert text_refused.returncode != 0, text_refused.stderr
            assert b'escape' in text_refused.stderr.lower(), text_refused.stderr
            assert text_allowed.returncode == 0, text_allowed.stderr
            assert text_allowed.stdout == textual
            assert observed == ['/binary', '/binary', '/text', '/text']
        finally:
            server.shutdown(); thread.join(timeout=5)
