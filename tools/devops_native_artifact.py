"""Published DevOps native intake. Source prerequisite, never release admission.

Expected identity is supplied by the trusted caller separately from registry
observations. This tool selects/adopts no version and issues no cloud action.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import tarfile
import tempfile
import threading
import urllib.request
import urllib.error
import zipfile

REPOSITORY = 'honua-io/honua-devops'
WORKFLOW = '.github/workflows/release-mcp.yml'
RIDS = {'linux-x64': ('tar.gz', 'Honua.DevOps.Agent'),
        'osx-x64': ('tar.gz', 'Honua.DevOps.Agent'),
        'osx-arm64': ('tar.gz', 'Honua.DevOps.Agent'),
        'win-x64': ('zip', 'Honua.DevOps.Agent.exe')}
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 256

class Refusal(ValueError):
    """Safe category only: never include URLs, environment or credential values."""

def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()

def require(condition, category):
    if not condition:
        raise Refusal(category)

def expected_identity(value):
    keys = {'repository', 'tag', 'sourceSha', 'rid', 'archiveSha256', 'releaseId', 'assetId'}
    require(isinstance(value, dict) and set(value) == keys, 'native-identity-shape')
    require(value['repository'] == REPOSITORY, 'native-repository')
    require(isinstance(value['tag'], str) and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', value['tag']), 'native-tag')
    require(isinstance(value['sourceSha'], str) and re.fullmatch('[0-9a-f]{40}', value['sourceSha']), 'native-source')
    require(isinstance(value['rid'], str) and value['rid'] in RIDS, 'native-rid')
    require(isinstance(value['archiveSha256'], str) and re.fullmatch('[0-9a-f]{64}', value['archiveSha256']), 'native-integrity')
    require(all(type(value[k]) is int and value[k] > 0 for k in ('releaseId', 'assetId')), 'native-registry-identity')
    return dict(value)

def archive_name(expected):
    return 'honua-devops-' + expected['rid'] + '.' + RIDS[expected['rid']][0]

def verify_publication(expected, release, tag_commit):
    expected_identity(expected)
    require(isinstance(release, dict) and type(release.get('id')) is int and release.get('id') == expected['releaseId']
            and release.get('tag_name') == expected['tag'] and release.get('draft') is False
            and isinstance(release.get('published_at'), str) and bool(release['published_at']), 'native-unpublished')
    require(tag_commit == expected['sourceSha'], 'native-tag-source')
    assets = release.get('assets')
    require(isinstance(assets, list), 'native-assets')
    selected = [a for a in assets if isinstance(a, dict) and a.get('name') == archive_name(expected)]
    checksums = [a for a in assets if isinstance(a, dict) and a.get('name') == archive_name(expected) + '.sha256']
    require(len(selected) == len(checksums) == 1, 'native-asset-ambiguity')
    asset = selected[0]
    require(type(asset.get('id')) is int and asset.get('id') == expected['assetId'] and asset.get('state') == 'uploaded'
            and type(asset.get('size')) is int and 0 < asset['size'] <= MAX_ARCHIVE, 'native-asset-identity')
    # Some older GitHub assets predate the digest API field. The independently
    # expected digest, downloaded bytes, checksum and provenance remain required.
    require(asset.get('digest') in (None, 'sha256:' + expected['archiveSha256']), 'native-registry-digest')
    checksum = checksums[0]
    require(type(checksum.get('id')) is int and checksum['id'] > 0
            and checksum.get('state') == 'uploaded' and type(checksum.get('size')) is int
            and 0 < checksum['size'] <= 4096, 'native-checksum-identity')
    return asset, checksum

def verify_archive(expected, archive, checksum):
    require(archive.is_file() and not archive.is_symlink() and 0 < archive.stat().st_size <= MAX_ARCHIVE, 'native-archive-shape')
    require(digest(archive) == expected['archiveSha256'], 'native-archive-integrity')
    try:
        text = checksum.decode('ascii')
    except (UnicodeError, AttributeError) as error:
        raise Refusal('native-checksum-format') from error
    require(text.strip() == expected['archiveSha256'] + '  ' + archive_name(expected)
            and len(text.strip().splitlines()) == 1, 'native-checksum-mismatch')

def validate_native_header(data, rid):
    if rid == 'linux-x64':
        require(len(data) >= 64 and data[:6] == b'\x7fELF\x02\x01'
                and struct.unpack_from('<H', data, 18)[0] == 62, 'native-elf-rid')
    elif rid.startswith('osx-'):
        require(len(data) >= 32 and data[:4] == b'\xcf\xfa\xed\xfe'
                and struct.unpack_from('<I', data, 4)[0] == (0x100000C if rid == 'osx-arm64' else 0x1000007), 'native-macho-rid')
    else:
        require(len(data) >= 64 and data[:2] == b'MZ', 'native-pe-rid')
        offset = struct.unpack_from('<I', data, 60)[0]
        require(offset + 6 <= len(data) and data[offset:offset + 4] == b'PE\0\0'
                and struct.unpack_from('<H', data, offset + 4)[0] == 0x8664, 'native-pe-rid')

def extract_sealed(expected, archive, destination):
    """Finite regular members only; no tar extraction/path-following primitive."""
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    seen = set()
    files = []
    total = 0
    entries = 0
    binary_name = RIDS[expected['rid']][1]
    def save(name, size, stream, directory=False):
        nonlocal total, entries
        entries += 1
        require(entries <= MAX_MEMBERS, 'native-member-count')
        require(type(size) is int and size >= 0 and (not directory or size == 0), 'native-member-size')
        # tar's conventional './' prefix is allowed, not parent/absolute paths.
        while name.startswith('./'):
            name = name[2:]
        if name in ('', '.') and directory:
            return
        path = PurePosixPath(name)
        require(name and not path.is_absolute() and '\\' not in name and '\x00' not in name
                and all(p not in ('', '.', '..') and ':' not in p and not p.endswith(('.', ' ')) for p in name.rstrip('/').split('/')), 'native-member-path')
        canonical = str(path)
        require(canonical not in seen and len(seen) < MAX_MEMBERS, 'native-member-duplicate')
        seen.add(canonical)
        target = destination.joinpath(*path.parts)
        require(type(size) is int and size >= 0, 'native-member-size')
        total += size
        require(total <= MAX_EXPANDED, 'native-expanded-bound')
        if directory:
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            return
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open('xb') as output:
            remaining = size
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                require(bool(block), 'native-member-truncated')
                output.write(block)
                remaining -= len(block)
            require(not stream.read(1), 'native-member-size')
        target.chmod(0o700 if canonical == binary_name else 0o600)
        files.append({'path': canonical, 'sha256': digest(target), 'bytes': size})
    try:
        if expected['rid'] == 'win-x64':
            with zipfile.ZipFile(archive) as source:
                for item in source.infolist():
                    mode = item.external_attr >> 16
                    require(not stat.S_ISLNK(mode) and (not stat.S_IFMT(mode) or stat.S_ISREG(mode) or stat.S_ISDIR(mode)), 'native-special-member')
                    with source.open(item) as stream:
                        save(item.filename, item.file_size, stream, item.is_dir())
        else:
            with tarfile.open(archive, 'r:gz') as source:
                for item in source:
                    require(item.isfile() or item.isdir(), 'native-special-member')
                    if item.isdir():
                        save(item.name, item.size, None, True)
                    else:
                        stream = source.extractfile(item)
                        require(stream is not None, 'native-member-stream')
                        with stream:
                            save(item.name, item.size, stream)
        binary = destination / binary_name
        require(binary.is_file() and not binary.is_symlink(), 'native-binary-missing')
        with binary.open('rb') as stream:
            validate_native_header(stream.read(1024 * 1024), expected['rid'])
        return sorted(files, key=lambda row: row['path'])
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as error:
        raise Refusal('native-archive-invalid') from error

def retained_run(command, stdout, stderr, timeout, *, stdout_bound=8 * 1024 * 1024,
                 stderr_bound=1024 * 1024, capture_stdout=True):
    """Bound the actual pipes/files, not a post-exit in-memory buffer.

    Production callers are fixed read-only gh operations. Only that owned read
    subprocess may be stopped on a limit/timeout; this never owns an actuator.
    """
    limits = []
    faults = []
    with stdout.open('xb') as output, stderr.open('xb') as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def pump(pipe, target, bound, label):
            written = 0
            try:
                while block := pipe.read1(64 * 1024):
                    allowed = min(len(block), max(0, bound - written))
                    if allowed:
                        target.write(block[:allowed]); target.flush(); written += allowed
                    if allowed != len(block):
                        if label not in limits:
                            limits.append(label)
                        # Popen retains the owned process identity and rechecks
                        # poll state; no caller PID or process group is signalled.
                        if process.poll() is None:
                            try: process.terminate()
                            except ProcessLookupError: pass
            except OSError:
                faults.append(label)
            finally:
                pipe.close()
        threads = [threading.Thread(target=pump, args=(process.stdout, output, stdout_bound, 'stdout'), daemon=True),
                   threading.Thread(target=pump, args=(process.stderr, errors, stderr_bound, 'stderr'), daemon=True)]
        for thread in threads: thread.start()
        timed_out = False
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try: code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); code = process.wait(timeout=2)
        for thread in threads: thread.join(timeout=2)
        require(not any(thread.is_alive() for thread in threads), 'native-read-pipe-not-terminal')
    require(not faults, 'native-read-stream-error')
    require(not limits, 'native-' + (limits[0] if limits else 'output') + '-limit')
    require(not timed_out, 'native-read-timeout')
    return subprocess.CompletedProcess(command, code, stdout.read_bytes() if capture_stdout else b'', stderr.read_bytes())


def gh_json(route, output):
    result = retained_run(['gh', 'api', '--hostname', 'github.com', route], output, output.with_suffix(output.suffix + '.stderr'), 60)
    require(result.returncode == 0, 'native-registry-unavailable')
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise Refusal('native-registry-response') from error


def resolve_tag(ref, directory):
    obj = gh_json(f'repos/{REPOSITORY}/git/ref/tags/{ref}', directory / 'tag-ref.json').get('object')
    seen = set()
    for number in range(4):
        require(isinstance(obj, dict) and isinstance(obj.get('sha'), str)
                and re.fullmatch('[0-9a-f]{40}', obj['sha']) and obj['sha'] not in seen, 'native-tag-object')
        seen.add(obj['sha'])
        if obj.get('type') == 'commit':
            return obj['sha']
        require(obj.get('type') == 'tag', 'native-tag-object')
        obj = gh_json(f"repos/{REPOSITORY}/git/tags/{obj['sha']}", directory / f'tag-object-{number}.json').get('object')
    raise Refusal('native-tag-depth')

def download(asset, target, bound):
    require(type(asset.get('size')) is int and 0 < asset['size'] <= bound, 'native-download-size')
    result = retained_run(['gh', 'api', '--hostname', 'github.com', f"repos/{REPOSITORY}/releases/assets/{asset['id']}",
                           '-H', 'Accept: application/octet-stream', '--allow-escape-sequences'], target,
                          target.with_suffix(target.suffix + '.stderr'), 180,
                          stdout_bound=asset['size'], capture_stdout=False)
    require(result.returncode == 0 and target.stat().st_size == asset['size'], 'native-download')


def stage(expected, output):
    expected = expected_identity(expected)
    require(output.is_dir() and not output.is_symlink(), 'native-output-root')
    # Private request-owned directory, not a global executable cache.
    request = Path(tempfile.mkdtemp(prefix='native-', dir=output))
    request.chmod(0o700)
    try:
        release = gh_json(f"repos/{REPOSITORY}/releases/tags/{expected['tag']}", request / 'release.json')
        tag_sha = resolve_tag(expected['tag'], request)
        asset, checksum = verify_publication(expected, release, tag_sha)
        archive = request / archive_name(expected)
        download(asset, archive, MAX_ARCHIVE)
        download(checksum, request / (archive.name + '.sha256'), 4096)
        verify_archive(expected, archive, (request / (archive.name + '.sha256')).read_bytes())
        # This is the repository's normal authenticated provenance route. The
        # command performs signature/certificate verification; raw JSON is never
        # accepted from the caller as proof that verification occurred.
        command = ['gh', 'attestation', 'verify', str(archive), '--hostname', 'github.com', '--repo', REPOSITORY,
                   '--signer-workflow', REPOSITORY + '/' + WORKFLOW,
                   '--source-ref', 'refs/tags/' + expected['tag'], '--source-digest', expected['sourceSha'],
                   '--deny-self-hosted-runners', '--format', 'json']
        result = retained_run(command, request / 'provenance.json', request / 'provenance.stderr', 180)
        require(result.returncode == 0, 'native-provenance-unverified')
        try:
            verified = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise Refusal('native-provenance-response') from error
        require(isinstance(verified, list) and len(verified) > 0, 'native-provenance-response')
        # gh binds the artifact digest and certificate identity using the flags
        # above. Preserve its complete original result for consumer re-verifying.
        require(digest(archive) == expected['archiveSha256'], 'native-after-verification-drift')
        members = extract_sealed(expected, archive, request / 'installed')
        binary = request / 'installed' / RIDS[expected['rid']][1]
        seal = {'schema': 'honua.devops-native-intake/v1', 'expected': expected,
                'requestDirectory': str(request), 'binary': str(binary), 'members': members,
                'archiveSha256': digest(archive), 'provenanceSha256': digest(request / 'provenance.json'),
                'publicationMetadataSha256': digest(request / 'release.json'),
                'sourceQualified': False, 'candidateQualified': False}
        (request / 'intake.json').write_text(json.dumps(seal, indent=2) + '\n')
        return seal
    except Exception as error:
        category = str(error) if isinstance(error, Refusal) else type(error).__name__
        (request / 'failure.json').write_text(json.dumps({'status': 'unqualified', 'category': category,
                                                       'candidateQualified': False}) + '\n')
        raise

SDK_LOCK_SHA256 = '5e6b28f4c29b82b799190e9e92083c7aca38b879afc7e242bee4320a711e195f'
SDK_URL = 'https://registry.npmjs.org/@modelcontextprotocol/sdk/-/sdk-1.32.1.tgz'
SDK_ARCHIVE_BOUND = 32 * 1024 * 1024


def verify_sdk_bytes(installed, archive, integrity):
    sha = hashlib.sha512()
    with archive.open('rb') as source:
        for block in iter(lambda: source.read(64 * 1024), b''): sha.update(block)
    require('sha512-' + base64.b64encode(sha.digest()).decode() == integrity, 'native-sdk-archive-integrity')
    expected = {}; directories = set(); count = total = 0
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            count += 1
            require(count <= 4096 and member.size >= 0 and member.size <= SDK_ARCHIVE_BOUND
                    and (member.isfile() or member.isdir()) and (not member.isdir() or member.size == 0), 'native-sdk-member')
            name = member.name
            require(name == 'package' or name.startswith('package/'), 'native-sdk-member-path')
            if name == 'package':
                require(member.isdir(), 'native-sdk-member-path'); continue
            relative = name[len('package/'):]
            parts = relative.rstrip('/').split('/')
            require(all(x not in ('', '.', '..') and ':' not in x and '\\' not in x and not x.endswith(('.', ' ')) for x in parts), 'native-sdk-member-path')
            relative = '/'.join(parts)
            total += member.size
            require(total <= 128 * 1024 * 1024, 'native-sdk-expanded-bound')
            for parent in PurePosixPath(relative).parents:
                if str(parent) != '.': directories.add(str(parent))
            if member.isdir():
                require(relative not in expected, 'native-sdk-duplicate'); directories.add(relative); continue
            require(relative not in expected and relative not in directories, 'native-sdk-duplicate')
            stream = source.extractfile(member); require(stream is not None, 'native-sdk-member')
            content = hashlib.sha256(); size = 0
            with stream:
                while block := stream.read(64 * 1024): content.update(block); size += len(block)
            require(size == member.size, 'native-sdk-member-size')
            expected[relative] = {'sha256': content.hexdigest(), 'bytes': size}
    require('package.json' in expected and bool(expected), 'native-sdk-metadata')
    require(installed.is_dir() and not installed.is_symlink(), 'native-sdk-installed-root')
    actual = {}; actual_dirs = set()
    for root, dirs, files in os.walk(installed, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name; info = path.lstat()
            require(not stat.S_ISLNK(info.st_mode), 'native-sdk-installed-link')
            relative = path.relative_to(installed).as_posix()
            if stat.S_ISDIR(info.st_mode): actual_dirs.add(relative)
            else:
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'native-sdk-installed-member')
                actual[relative] = {'sha256': digest(path), 'bytes': info.st_size}
    require(actual == expected and actual_dirs == directories, 'native-sdk-installed-drift')
    metadata = json.loads((installed / 'package.json').read_text())
    require(metadata.get('name') == '@modelcontextprotocol/sdk' and metadata.get('version') == '1.32.1', 'native-sdk-metadata')
    return {'package': '@modelcontextprotocol/sdk', 'version': '1.32.1', 'integrity': integrity,
            'closureSha256': hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(), 'fileCount': len(expected)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refusal('native-sdk-registry-redirect')


def verify_installed_sdk(output, original_archive=None):
    root = Path(__file__).resolve().parents[1] / 'certification/sources/geospatial-mcp'
    lock = root / 'package-lock.json'
    require(digest(lock) == SDK_LOCK_SHA256, 'native-sdk-lock')
    package = json.loads(lock.read_text())['packages']['node_modules/@modelcontextprotocol/sdk']
    require(package.get('version') == '1.32.1' and package.get('resolved') == SDK_URL, 'native-sdk-lock')
    request = Path(tempfile.mkdtemp(prefix='sdk-', dir=output)); request.chmod(0o700)
    archive = Path(original_archive) if original_archive else request / 'sdk.tgz'
    try:
        if original_archive is None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(SDK_URL, timeout=30) as response, archive.open('xb') as stream:
                total = 0
                while block := response.read(64 * 1024):
                    allowed = min(len(block), max(0, SDK_ARCHIVE_BOUND - total))
                    if allowed: stream.write(block[:allowed]); total += allowed
                    require(allowed == len(block), 'native-sdk-download-limit')
        require(archive.is_file() and not archive.is_symlink() and archive.stat().st_size <= SDK_ARCHIVE_BOUND, 'native-sdk-archive-shape')
        result = verify_sdk_bytes(root / 'node_modules/@modelcontextprotocol/sdk', archive, package['integrity'])
        result.update({'archive': str(archive.resolve()), 'archiveSha256': digest(archive), 'lockSha256': SDK_LOCK_SHA256,
                       'candidateQualified': False})
        (request / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
        return result
    except Exception as error:
        (request / 'failure.json').write_text(json.dumps({'status': 'unqualified', 'candidateQualified': False,
                                                       'category': str(error) if isinstance(error, Refusal) else type(error).__name__}) + '\n')
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--expected')
    mode.add_argument('--verify-sdk', action='store_true')
    parser.add_argument('--sdk-archive')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.verify_sdk:
            print(json.dumps(verify_installed_sdk(Path(args.output).resolve(), args.sdk_archive)))
        else:
            require(args.sdk_archive is None, 'native-cli-shape')
            expected = json.loads(Path(args.expected).read_text())
            print(json.dumps(stage(expected, Path(args.output).resolve())))
        return 0
    except (Refusal, OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'unqualified', 'category': str(error) if isinstance(error, Refusal)
                          else type(error).__name__, 'candidateQualified': False}))
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
