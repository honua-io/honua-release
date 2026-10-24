"""Fixed published DevOps launcher and read-only Linux ownership census.

No shell/command selector, signal, cancellation, executor deadline or cloud call.
The normal intake producer authenticates publication; this launcher rechecks its
original bytes and fd-execs only its fixed linux-x64 member with --mcp.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import threading
import time
import tarfile

from devops_native_artifact import expected_identity, validate_native_header, verify_archive, archive_name

SCHEMA = 'honua.owned-process-scope/v1'
REQUEST_SCHEMA = 'honua.published-native-scope-request/v1'
MAX_PROCESSES = 32768
MAX_ORIGINAL = 64 * 1024 * 1024
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


class Refusal(ValueError):
    """Safe category only; no environment or foreign process arguments."""


def require(value, code):
    if not value:
        raise Refusal(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    result = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def bounded(path, limit):
    with open(path, 'rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, 'scope-original-bound')
    return raw


def private_root(value):
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path, 'scope-private-path')
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0, 'scope-private-owner')
    return path


def owned_file(path, root, limit, private=False):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and path.is_relative_to(root), 'scope-file-path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and info.st_mode & (0o077 if private else 0o022) == 0
                and 0 < info.st_size <= limit, 'scope-file-owner')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        require(len(raw) == info.st_size and info.st_mtime_ns == after.st_mtime_ns
                and info.st_size == after.st_size, 'scope-file-raced')
        return raw
    finally:
        os.close(fd)


def private_write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def parse_stat(raw, pid):
    require(type(pid) is int and pid > 0 and isinstance(raw, bytes), 'scope-stat-input')
    close = raw.rfind(b')')
    require(raw.startswith(str(pid).encode() + b' (') and close > 0, 'scope-stat-pid')
    fields = raw[close + 1:].split()
    require(len(fields) >= 20 and len(fields[0]) == 1
            and all(fields[i].isdigit() for i in (1, 2, 3, 19)), 'scope-stat-shape')
    require(int(fields[19]) > 0, 'scope-stat-birth')
    return {'pid': pid, 'parentPid': int(fields[1]), 'processGroup': int(fields[2]),
            'sessionId': int(fields[3]), 'startTicks': fields[19].decode(), 'state': fields[0].decode()}


def observation(pid, command=True):
    try:
        first = bounded(f'/proc/{pid}/stat', 65536)
    except (FileNotFoundError, ProcessLookupError):
        return {'pid': pid, 'status': 'absent', 'statBase64': None, 'cmdlineBase64': None}
    before = parse_stat(first, pid)
    raw_command = bounded(f'/proc/{pid}/cmdline', 2 * 1024 * 1024) if command else None
    last = bounded(f'/proc/{pid}/stat', 65536)
    after = parse_stat(last, pid)
    require(all(before[key] == after[key] for key in ('parentPid', 'processGroup', 'sessionId', 'startTicks')),
            'scope-process-raced')
    result = {'pid': pid, 'status': 'observed' if command else 'stat-only',
              'statBase64': base64.b64encode(last).decode(),
              'cmdlineBase64': None if raw_command is None else base64.b64encode(raw_command).decode()}
    return result


def admission(pid, boot_id):
    row = observation(pid)
    require(row['status'] == 'observed', 'scope-admission-absent')
    parsed = parse_stat(base64.b64decode(row['statBase64']), pid)
    command = base64.b64decode(row['cmdlineBase64'])
    return {'schema': 'honua.owned-process-observation/v1', 'bootId': boot_id, **row,
            'identity': {key: parsed[key] for key in ('pid', 'parentPid', 'startTicks', 'state')}
            | {'cmdlineSha256': sha(command)}}


def establish_scope():
    require(sys.platform == 'linux', 'scope-linux-required')
    if os.getsid(0) != os.getpid():
        os.setsid()
    require(os.getpgrp() == os.getsid(0) == os.getpid(), 'scope-private-session')
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0, 'scope-subreaper-set')
    value = ctypes.c_int()
    require(libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0) == 0
            and value.value == 1, 'scope-subreaper-readback')
    return value.value


def owned_members(rows, launcher):
    parsed = {row['pid']: parse_stat(base64.b64decode(row['statBase64']), row['pid'])
              for row in rows if row['status'] != 'absent'}
    launcher_stat = parsed.get(launcher['pid'])
    require(launcher_stat is not None and launcher_stat['startTicks'] == launcher['identity']['startTicks']
            and launcher_stat['sessionId'] == launcher_stat['processGroup'] == launcher['pid'],
            'scope-launcher-birth')
    members = set()
    for pid, value in parsed.items():
        if value['sessionId'] == launcher['pid']:
            members.add(pid)
            continue
        current, seen = pid, set()
        while current in parsed and current not in seen:
            if current == launcher['pid']:
                members.add(pid)
                break
            seen.add(current)
            current = parsed[current]['parentPid']
    return members


def task_children(members):
    records = []
    for pid in sorted(members):
        tasks = sorted(int(path.name) for path in Path(f'/proc/{pid}/task').iterdir() if path.name.isdigit())
        require(len(tasks) <= MAX_PROCESSES, 'scope-task-bound')
        for tid in tasks:
            raw = bounded(f'/proc/{pid}/task/{tid}/children', 1024 * 1024)
            text = raw.decode('ascii').strip()
            require(not text or re.fullmatch(r'[1-9][0-9]*(?:\s+[1-9][0-9]*)*', text), 'scope-children-shape')
            children = [] if not text else [int(value) for value in text.split()]
            require(len(children) == len(set(children)), 'scope-children-duplicate')
            records.append({'pid': pid, 'tid': tid, 'rawBase64': base64.b64encode(raw).decode()})
    return records


def validate_children(records, members, rows):
    parsed = {row['pid']: parse_stat(base64.b64decode(row['statBase64']), row['pid'])
              for row in rows if row['status'] != 'absent'}
    for record in records:
        text = base64.b64decode(record['rawBase64']).decode('ascii').strip()
        for child in ([] if not text else [int(value) for value in text.split()]):
            require(child in members and child in parsed
                    and parsed[child]['parentPid'] == record['pid'], 'scope-child-census-raced')


def census(launcher, native, nonce, subreaper, launch_binding):
    require(os.getpid() == launcher['pid'] and subreaper == 1, 'scope-census-owner')
    boot = bounded('/proc/sys/kernel/random/boot_id', 128).decode('ascii').strip()
    require(boot == launcher['bootId'] == native['bootId'], 'scope-boot-drift')
    enumerated = sorted(int(path.name) for path in Path('/proc').iterdir() if path.name.isdigit())
    require(0 < len(enumerated) <= MAX_PROCESSES, 'scope-pid-bound')
    pids = sorted(set(enumerated) | {launcher['pid'], native['pid']})
    rows = [observation(pid, command=False) for pid in pids]
    members = owned_members(rows, launcher)
    before = task_children(members)
    validate_children(before, members, rows)
    # Foreign argv is deliberately never collected. Every exclusion is derived
    # from the complete raw stat census, not a caller-supplied membership flag.
    for index, row in enumerate(rows):
        if row['pid'] in members:
            current = observation(row['pid'])
            first_stat = parse_stat(base64.b64decode(row['statBase64']), row['pid'])
            require(current['status'] == 'observed', 'scope-census-raced')
            last_stat = parse_stat(base64.b64decode(current['statBase64']), row['pid'])
            require(all(first_stat[key] == last_stat[key] for key in
                        ('parentPid', 'processGroup', 'sessionId', 'startTicks')), 'scope-census-raced')
            rows[index] = current
    after = task_children(members)
    validate_children(after, members, rows)
    require(before == after, 'scope-children-raced')
    current_launcher = admission(launcher['pid'], boot)
    current_native = observation(native['pid'])
    require(current_launcher['identity']['startTicks'] == launcher['identity']['startTicks'], 'scope-launcher-reused')
    result = {'schema': SCHEMA, 'nonce': nonce, 'bootId': boot, 'launchBinding': launch_binding, 'launcherAdmission': launcher,
              'pidNamespace': os.readlink('/proc/self/ns/pid'),
              'nativeAdmission': native, 'subreaperReadback': subreaper,
              'launcherCwd': os.readlink('/proc/self/cwd'),
              'launcherStatusBase64': base64.b64encode(bounded('/proc/self/status', 65536)).decode(),
              'enumeratedPids': enumerated, 'observations': rows, 'taskChildrenBefore': before, 'taskChildrenAfter': after,
              'nativeCurrent': current_native, 'capturedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'monotonicNs': str(time.monotonic_ns())}
    require(len(json.dumps(result).encode()) <= MAX_ORIGINAL, 'scope-census-byte-bound')
    return result


def quiescent(original):
    members = owned_members(original['observations'], original['launcherAdmission'])
    allowed = {(row['pid'], row['identity']['startTicks'])
               for row in (original['launcherAdmission'], original['nativeAdmission'])}
    for row in original['observations']:
        if row['pid'] not in members:
            continue
        value = parse_stat(base64.b64decode(row['statBase64']), row['pid'])
        if value['state'] not in ('Z', 'X') and (value['pid'], value['startTicks']) not in allowed:
            return False
    return True


def verify_native_member(binary, root, member, archive):
    require(binary.resolve() == binary and binary.is_relative_to(root), 'scope-native-path')
    fd = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and info.st_mode & 0o077 == 0 and info.st_mode & stat.S_IXUSR
                and 64 <= info.st_size <= 2 * 1024 * 1024 * 1024, 'scope-native-owner')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            validate_native_header(stream.read(64), 'linux-x64')
            stream.seek(0)
            hasher = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(block)
        after = os.fstat(fd)
        require(hasher.hexdigest() == member['sha256'] and info.st_size == member['bytes']
                and info.st_size == after.st_size and info.st_mtime_ns == after.st_mtime_ns,
                'scope-native-byte-drift')
    finally:
        os.close(fd)
    # A forged intake member cannot select a workspace-built binary: its bytes
    # must also equal the fixed member of the hash-verified published archive.
    count = total = matches = 0
    with tarfile.open(archive, 'r:gz') as source:
        for entry in source:
            count += 1
            total += entry.size
            require(count <= 256 and total <= 2 * 1024 * 1024 * 1024
                    and entry.size >= 0 and (not entry.isdir() or entry.size == 0), 'scope-archive-bound')
            name = entry.name
            while name.startswith('./'):
                name = name[2:]
            if name != 'Honua.DevOps.Agent':
                continue
            matches += 1
            require(entry.isfile() and matches == 1 and entry.size == member['bytes'], 'scope-archive-member')
            stream = source.extractfile(entry)
            require(stream is not None, 'scope-archive-member')
            hasher = hashlib.sha256()
            with stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(block)
            require(hasher.hexdigest() == member['sha256'], 'scope-archive-installed-drift')
    require(matches == 1, 'scope-archive-member-missing')


def verify_request(path):
    request_path = Path(path)
    scope = private_root(request_path.parent)
    request_raw = owned_file(request_path, scope, 65536, private=True)
    request = json.loads(request_raw)
    require(set(request) == {'schema', 'nonce', 'intakePath', 'intakeSha256', 'scopeDirectory'}
            and request['schema'] == REQUEST_SCHEMA and re.fullmatch('[a-f0-9]{64}', request['nonce'])
            and request['scopeDirectory'] == str(scope), 'scope-request-shape')
    intake_path = Path(request['intakePath'])
    intake_root = private_root(intake_path.parent)
    raw = owned_file(intake_path, intake_root, 1024 * 1024)
    require(sha(raw) == request['intakeSha256'], 'scope-intake-original')
    intake = json.loads(raw)
    require(intake.get('schema') == 'honua.devops-native-intake/v1'
            and intake.get('sourceQualified') is False and intake.get('candidateQualified') is False
            and intake.get('requestDirectory') == str(intake_root), 'scope-intake-shape')
    expected = expected_identity(intake['expected'])
    require(expected['rid'] == 'linux-x64', 'scope-native-rid')
    archive = intake_root / archive_name(expected)
    verify_archive(expected, archive, owned_file(Path(str(archive) + '.sha256'), intake_root, 4096))
    require(sha(owned_file(intake_root / 'provenance.json', intake_root, 2 * 1024 * 1024)) == intake['provenanceSha256']
            and sha(owned_file(intake_root / 'release.json', intake_root, 2 * 1024 * 1024)) == intake['publicationMetadataSha256'],
            'scope-intake-provenance-drift')
    binary = intake_root / 'installed' / 'Honua.DevOps.Agent'
    require(intake.get('binary') == str(binary), 'scope-fixed-native-member')
    members = intake.get('members')
    require(isinstance(members, list) and 0 < len(members) <= 256, 'scope-native-members')
    selected = [row for row in members if row.get('path') == 'Honua.DevOps.Agent']
    require(len(selected) == 1, 'scope-native-member-ambiguity')
    verify_native_member(binary, intake_root, selected[0], archive)
    # The fd used for exec is checked again below. No caller chooses executable
    # name, argument vector, shell, working directory or alternative actuator.
    return request, intake, binary, selected[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    request, intake, binary, member = verify_request(args.request)
    scope = Path(request['scopeDirectory'])
    subreaper = establish_scope()
    boot = bounded('/proc/sys/kernel/random/boot_id', 128).decode('ascii').strip()
    launcher = admission(os.getpid(), boot)
    launch_binding = {'schema': 'honua.published-native-scope-binding/v1',
                      'requestSha256': sha(bounded(args.request, 65536)),
                      'intakeSha256': request['intakeSha256'], 'nativeSha256': member['sha256'],
                      'nativePath': str(binary), 'nativeArgv': [str(binary), '--mcp'],
                      'sourceSha': intake['expected']['sourceSha'], 'rid': 'linux-x64',
                      'scopeDirectory': str(scope), 'launcherSha256': sha(bounded(__file__, 1024 * 1024)),
                      'interpreterPath': os.readlink('/proc/self/exe'),
                      'interpreterSha256': file_sha('/proc/self/exe')}
    private_write(scope / 'launch-binding.json', json.dumps(launch_binding).encode() + b'\n')
    socket_path = scope / 'control.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original_cwd = os.getcwd()
    os.chdir(scope)
    # The short proc cwd alias avoids Unix-domain path-length limits without
    # moving the private control channel into a shared /tmp directory.
    listener.bind('control.sock')
    os.chmod(socket_path, 0o600)
    listener.listen(4)
    native_input, forwarded_input = os.pipe()
    fd = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(os.dup(fd), 'rb') as stream:
        hasher = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    require(hasher.hexdigest() == member['sha256'] and os.fstat(fd).st_size == member['bytes'], 'scope-exec-fd-drift')
    os.lseek(fd, 0, os.SEEK_SET)
    child = os.fork()
    if child == 0:
        listener.close()
        os.close(forwarded_input)
        os.dup2(native_input, 0)
        os.close(native_input)
        os.chdir(original_cwd)
        os.execve(fd, [str(binary), '--mcp'], dict(os.environ))
        os._exit(126)
    os.close(fd)
    os.close(native_input)
    native = None
    stop = threading.Event()
    input_lock = threading.Lock()

    def forward():
        # Opaque pass-through only. Unexpected parent EOF cannot cancel an
        # admitted native action; only the explicit verified drain closes input.
        while True:
            block = os.read(0, 65536)
            if not block:
                try:
                    private_write(scope / 'parent-input-ended.json', b'{"status":"parent-input-ended-native-retained"}\n')
                except FileExistsError:
                    pass
                return
            with input_lock:
                if forwarded_input is None:
                    return
                try:
                    view = memoryview(block)
                    while view:
                        view = view[os.write(forwarded_input, view):]
                except BrokenPipeError:
                    return

    def serve():
        nonlocal native, forwarded_input
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                try:
                    peer = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    require(peer[1] == os.getuid(), 'scope-control-owner')
                    raw = bytearray()
                    deadline = time.monotonic() + 5
                    while not raw.endswith(b'\n'):
                        remaining = deadline - time.monotonic()
                        require(remaining > 0, 'scope-control-read-expired')
                        connection.settimeout(remaining)
                        block = connection.recv(min(4097 - len(raw), 4097))
                        require(bool(block), 'scope-control-read-incomplete')
                        raw.extend(block)
                        require(len(raw) <= 4096, 'scope-control-bound')
                    require(len(raw) <= 4096, 'scope-control-bound')
                    query = json.loads(raw)
                    require(isinstance(query, dict) and set(query) == {'schema', 'nonce', 'action'}
                            and query['schema'] == SCHEMA and query['nonce'] == request['nonce']
                            and query['action'] in ('census', 'drain'), 'scope-control-request')
                    if native is None:
                        captured = admission(child, boot)
                        require(base64.b64decode(captured['cmdlineBase64']).split(b'\0') == [os.fsencode(binary), b'--mcp', b''],
                                'scope-native-command')
                        native = captured
                    original = census(launcher, native, request['nonce'], subreaper, launch_binding)
                    if query['action'] == 'drain':
                        require(quiescent(original), 'scope-drain-live-descendant')
                        with input_lock:
                            if forwarded_input is not None:
                                os.close(forwarded_input)
                                forwarded_input = None
                        response = {'status': 'draining', 'original': original}
                    else:
                        response = {'status': 'observed', 'original': original}
                except Exception as error:
                    response = {'status': 'unverified', 'category': str(error) if isinstance(error, Refusal) else type(error).__name__}
                try:
                    connection.sendall(json.dumps(response).encode() + b'\n')
                except OSError:
                    pass  # An observer disconnect never changes native ownership.

    server_failed = False
    try:
        threading.Thread(target=forward, daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()
    except RuntimeError:
        # Preserve the admitted child and wait for its actual normal completion;
        # a control-thread failure cannot release ownership by exiting early.
        server_failed = True
    native_exit = None
    reaped = []
    reaped_overflow = False
    # waitpid's actual no-children result is authoritative for launcher exit.
    # A setsid/double-fork escape remains a descendant or is adopted here.
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            if native is not None:
                try:
                    require(not reaped_overflow, 'scope-reaped-bound')
                    terminal = census(launcher, native, request['nonce'], subreaper, launch_binding)
                    terminal['waitpidNoChildren'] = True
                    terminal['reapedChildren'] = reaped
                    private_write(scope / 'scope-terminal.json', json.dumps({'status': 'observed', 'original': terminal}).encode() + b'\n')
                except Exception as error:
                    private_write(scope / 'scope-terminal-unverified.json', json.dumps({
                        'status': 'unverified', 'category': str(error) if isinstance(error, Refusal) else type(error).__name__
                    }).encode() + b'\n')
            break
        if pid:
            if len(reaped) < MAX_PROCESSES:
                reaped.append({'pid': pid, 'waitStatus': status, 'exitCode': os.waitstatus_to_exitcode(status)})
            else:
                reaped_overflow = True  # Retain ownership; never exit on journal overflow.
        if pid == child:
            native_exit = os.waitstatus_to_exitcode(status)
        if pid == 0:
            time.sleep(0.05)  # Observation cadence, never an executor deadline.
    stop.set()
    listener.close()
    socket_path.unlink()
    require(native_exit is not None, 'scope-native-exit-unobserved')
    return 1 if server_failed else native_exit


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(str(error) if isinstance(error, Refusal) else type(error).__name__, file=sys.stderr)
        sys.exit(1)
