"""Actual kernel originals through fixed data-only JS and Python join; no native/AWS credit."""
import base64
import copy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest
from devops_provisioning_contract import Refusal, canonical, sha256
from judge_devops_cleanup import ScopeContext, verify_owned_scope


def test_actual_scope_wrapper_closed_live_terminal_and_original_tamper():
    before = datetime.now(timezone.utc)
    child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('test_devops_native_scope.py')),
                              '--source-control-worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, bufsize=0)
    buffer = bytearray()
    def receive():
        end = time.monotonic() + 20
        while b'\n' not in buffer:
            ready, _, _ = select.select([child.stdout], [], [], max(0, end - time.monotonic()))
            assert ready, 'read-only source observer expired; ownership retained'
            raw = os.read(child.stdout.fileno(), 65536)
            assert raw, 'fixture ended without kernel original'
            buffer.extend(raw)
            assert len(buffer) < 64 * 1024 * 1024
        line, _, rest = buffer.partition(b'\n'); buffer[:] = rest
        return json.loads(line)
    def send(command):
        child.stdin.write((command + '\n').encode()); child.stdin.flush()
    def frame(record):
        value = copy.deepcopy(record['original'])
        # Explicit source-fixture context; no published intake/binary authority.
        expected = {name: copy.deepcopy(value[name]) for name in
                    ('launcherAdmission', 'nativeAdmission', 'nonce', 'pidNamespace', 'launchBinding')}
        expected['launchBinding'].update(scopeDirectory=value['launcherCwd'], nativeArgv=
            base64.b64decode(value['nativeAdmission']['cmdlineBase64']).decode().split('\0')[:-1])
        value['launchBinding'] = copy.deepcopy(expected['launchBinding'])
        raw = canonical({'status': 'observed', 'original': value})
        context = ScopeContext(sha256(raw), expected, before, datetime.now(timezone.utc) + timedelta(seconds=1))
        return raw, context
    try:
        live, context = frame(receive())
        with pytest.raises(Refusal, match='not-closed'): verify_owned_scope(live, context)
        send('release-escape'); quiet, context = frame(receive())
        result = verify_owned_scope(quiet, context)
        assert result['candidateQualified'] is False and result['nativeTerminal'] is False
        with pytest.raises(Refusal, match='native-terminal'): verify_owned_scope(quiet, context, native_terminal=True)
        send('drain'); slow, slow_context = frame(receive())
        with pytest.raises(Refusal, match='not-closed'): verify_owned_scope(slow, slow_context)
        terminal, context = frame(receive())
        result = verify_owned_scope(terminal, context, native_terminal=True)
        assert result['nativeTerminal'] is True and result['sixAwsCellsQualified'] is False
        assert receive()['waitedSeconds'] > 2
        child.stdin.close(); assert child.wait(timeout=20) == 0
        assert child.stderr.read() == b''
        for kind in ('missing-native', 'native-string', 'native-pid-bool', 'captured-type', 'wrong-custody',
                     'foreign-namespace', 'missing-reaped', 'wrong-reaped-status', 'missing-native-reap',
                     'missing-nochildren', 'extra-envelope', 'missing-census', 'wrong-monotonic'):
            value = json.loads(terminal); expected = copy.deepcopy(context.expected)
            if kind == 'missing-native': expected['nativeAdmission'] = None
            if kind == 'native-string': expected['nativeAdmission'] = 'invalid'
            if kind == 'native-pid-bool': expected['nativeAdmission']['pid'] = True
            original = value['original']
            if kind == 'captured-type': original['capturedAt'] = None
            if kind == 'foreign-namespace': expected['pidNamespace'] = 'pid:[1]'
            if kind == 'missing-reaped': original.pop('reapedChildren')
            if kind == 'wrong-reaped-status': original['reapedChildren'][0]['exitCode'] += 1
            if kind == 'missing-native-reap':
                original['reapedChildren'] = [row for row in original['reapedChildren']
                                             if row['pid'] != expected['nativeAdmission']['pid']]
            if kind == 'missing-nochildren': original.pop('waitpidNoChildren')
            if kind == 'extra-envelope': value['unexpected'] = True
            if kind == 'missing-census': original['observations'].pop()
            if kind == 'wrong-monotonic': original['monotonicNs'] = 'caller-trust'
            raw = canonical(value)
            changed = replace(context, expected=expected, original_sha256=sha256(raw))
            if kind == 'wrong-custody': changed = replace(changed, original_sha256='f' * 64)
            with pytest.raises(Refusal): verify_owned_scope(raw, changed, native_terminal=True)
    finally:
        if not child.stdin.closed: child.stdin.close()  # normal fixture release only
        child.wait(timeout=20)
        child.stdout.close(); child.stderr.close()
