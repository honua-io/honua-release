"""Real Linux kernel ownership controls; no published-native or AWS qualification."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest

import devops_native_scope as scope


def capture(launcher, native):
    # A raced read is never accepted. Retry only the read-only kernel census.
    last = None
    for _ in range(40):
        try:
            return scope.census(launcher, native, 'a' * 64, 1, {'sourceControlOnly': True})
        except (FileNotFoundError, ProcessLookupError, scope.Refusal) as error:
            last = error
            time.sleep(.025)
    raise last


def worker():
    scope.establish_scope()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    launcher = scope.admission(os.getpid(), boot)
    native_read, native_write = os.pipe()
    native = os.fork()
    if native == 0:
        os.close(native_write)
        os.read(native_read, 1)
        os.close(native_read)
        # Shutdown work is normal and deliberately exceeds stock SDK's 2s
        # close fallback. The harness must wait, never signal it.
        slow = os.fork()
        if slow == 0:
            time.sleep(2.5)
            os._exit(0)
        os._exit(0)
    os.close(native_read)
    admitted = scope.admission(native, boot)
    escape_read, escape_write = os.pipe()
    ready_read, ready_write = os.pipe()
    intermediate = os.fork()
    if intermediate == 0:
        os.close(escape_write)
        os.close(ready_read)
        escaped = os.fork()
        if escaped == 0:
            os.setsid()
            os.write(ready_write, str(os.getpid()).encode())
            os.close(ready_write)
            os.read(escape_read, 1)
            os._exit(0)
        os._exit(0)
    os.close(escape_read)
    os.close(ready_write)
    reaped = []
    def reap(pid, flags=0):
        observed, status = os.waitpid(pid, flags)
        if observed:
            reaped.append({'pid': observed, 'waitStatus': status, 'exitCode': os.waitstatus_to_exitcode(status)})
        return observed, status
    escaped = int(os.read(ready_read, 64))
    os.close(ready_read)
    reap(intermediate, 0)  # escape parent gone BEFORE first census
    def emit(kind):
        original = capture(launcher, admitted)
        if kind == 'terminal':
            original['waitpidNoChildren'] = True
            original['reapedChildren'] = list(reaped)
        print(json.dumps({'kind': kind, 'original': original, 'closed': scope.quiescent(original),
                          'escapedPid': escaped}), flush=True)
    try:
        emit('escaped-live')
        for line in sys.stdin:
            command = line.strip()
            if command == 'observe':
                emit('observation-only')
            elif command == 'release-escape':
                os.write(escape_write, b'x'); os.close(escape_write); escape_write = None
                reap(escaped, 0)
                emit('quiescent')
            elif command == 'drain':
                os.close(native_write); native_write = None
                reap(native, 0)
                emit('shutdown-child-live')
                started = time.monotonic()
                while True:
                    try:
                        reap(-1, 0)
                    except ChildProcessError:
                        break
                emit('terminal')
                print(json.dumps({'kind': 'normal-drain', 'waitedSeconds': time.monotonic() - started}), flush=True)
                return
            else:
                raise ValueError('source-control-command')
    finally:
        # Only normal pipe release and waitpid; no kill/terminate/timeout signal.
        for fd in (escape_write, native_write):
            if fd is not None:
                os.close(fd)
        while True:
            try:
                os.waitpid(-1, 0)
            except ChildProcessError:
                break


def initialize_error_worker(root):
    """Credential-free actual MCP error fixture, not product server evidence."""
    scope.establish_scope()
    release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(release_write)
        os.read(release_read, 1)
        os._exit(0)
    os.close(release_read)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    Path(root, 'child.json').write_text(json.dumps(scope.admission(child, boot)))
    # A separate release FIFO permits normal cleanup even if SDK has stopped
    # consuming its protocol stream. No kill/terminate or arbitrary command.
    fifo = Path(root, 'release.fifo')
    os.mkfifo(fifo, 0o600)
    def release():
        with fifo.open('rb', buffering=0) as stream:
            stream.read(1)
        os.close(release_write)
    import threading
    threading.Thread(target=release, daemon=True).start()
    for line in sys.stdin:
        request = json.loads(line)
        if request.get('method') == 'initialize':
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                              'error': {'code': -32603, 'message': 'owned-source-initialize-refusal'}}), flush=True)
            os.waitpid(child, 0)
            return


@unittest.skipUnless(sys.platform == 'linux', 'Linux kernel ownership contract')
class NativeScopeTests(unittest.TestCase):
    def test_actual_setsid_escape_and_slow_normal_shutdown(self):
        process = subprocess.Popen([sys.executable, __file__, '--source-control-worker'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   bufsize=0)
        buffered = bytearray()
        def receive():
            deadline = time.monotonic() + 20
            while b'\n' not in buffered:
                ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
                self.assertTrue(ready, 'source-control observer expired; owned process retained')
                block = os.read(process.stdout.fileno(), 65536)
                self.assertTrue(block, 'source-control worker exited without original')
                buffered.extend(block)
                self.assertLessEqual(len(buffered), scope.MAX_ORIGINAL)
            text, _, rest = buffered.partition(b'\n')
            buffered[:] = rest
            return json.loads(text)
        def send(value):
            process.stdin.write((value + '\n').encode()); process.stdin.flush()
        originals = []
        try:
            live = receive(); originals.append(live)
            self.assertFalse(live['closed'])
            original = live['original']
            rows = {row['pid']: row for row in original['observations']}
            escaped = scope.parse_stat(base64.b64decode(rows[live['escapedPid']]['statBase64']), live['escapedPid'])
            self.assertEqual(escaped['parentPid'], original['launcherAdmission']['pid'])
            self.assertEqual(escaped['sessionId'], escaped['pid'])
            # Observer abandonment does not signal the child or release custody.
            time.sleep(.1)
            send('observe'); still = receive(); originals.append(still)
            self.assertFalse(still['closed'])
            send('release-escape'); quiet = receive(); originals.append(quiet)
            self.assertTrue(quiet['closed'])
            send('drain'); shutdown = receive(); originals.append(shutdown)
            self.assertFalse(shutdown['closed'])
            terminal = receive(); originals.append(terminal)
            self.assertTrue(terminal['closed'])
            drained = receive()
            self.assertGreater(drained['waitedSeconds'], 2)
            process.stdin.close()
            self.assertEqual(process.wait(timeout=20), 0)
            self.assertEqual(process.stderr.read(), b'')
            output = os.environ.get('HONUA_SCOPE_CONTROL_ORIGINALS')
            if output:
                Path(output).write_text(json.dumps(originals) + '\n')
        finally:
            if not process.stdin.closed:
                process.stdin.close()  # normal fixture release only
            process.wait(timeout=20)
            process.stdout.close(); process.stderr.close()

    def test_raw_stat_birth_and_session_are_not_caller_flags(self):
        raw = Path('/proc/self/stat').read_bytes()
        current = scope.parse_stat(raw, os.getpid())
        self.assertEqual(current['sessionId'], os.getsid(0))
        self.assertEqual(current['processGroup'], os.getpgrp())
        with self.assertRaises(scope.Refusal):
            scope.parse_stat(raw, os.getpid() + 1)

    def test_children_missing_from_full_census_refuses(self):
        with self.assertRaises(scope.Refusal):
            scope.validate_children([{'pid': 1, 'tid': 1, 'rawBase64': base64.b64encode(b'2 ').decode()}], {1}, [])

    def test_scope_request_refuses_nonprivate_directory_before_intake(self):
        import tempfile
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); root.chmod(0o755)
            with self.assertRaisesRegex(scope.Refusal, 'scope-private-owner'):
                scope.verify_request(root / 'request.json')


if __name__ == '__main__':
    if '--initialize-error-worker' in sys.argv:
        initialize_error_worker(sys.argv[sys.argv.index('--initialize-error-worker') + 1])
    elif '--source-control-worker' in sys.argv:
        worker()
    else:
        unittest.main()
