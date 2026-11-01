"""tools/ai_arc_dev_candidate.sh with git/docker/ssh stubs: a hanging build client is not waited on."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / 'ai_arc_dev_candidate.sh'
SHA = 'a' * 40
ID = 'sha256:' + 'f' * 64


def stubs(tmp_path, build):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    state = tmp_path / 'built'
    (bin_dir / 'git').write_text('#!/bin/sh\nexit 0\n')
    (bin_dir / 'ssh').write_text('#!/bin/sh\nexit 1\n')       # no zstd: plain save/load fallback
    (bin_dir / 'docker').write_text(f'''#!/bin/bash
case "$1" in
  build) cat >/dev/null; {build};;
  image) case "$*" in
           *Labels*) [ -f {state} ] && echo {SHA}; exit 0;;
           *Id*) echo {ID};;
         esac;;
  save) echo image;;
  load) cat >/dev/null;;
esac
''')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir, state


def run(tmp_path, build, timeout='30'):
    bin_dir, state = stubs(tmp_path, build.replace('STATE', str(tmp_path / 'built')))
    env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', HONUA_DEV_BUILD_POLL='1',
               HONUA_DEV_BUILD_TIMEOUT=timeout)
    out = tmp_path / 'lock.json'
    done = subprocess.run(['bash', str(SCRIPT), str(tmp_path), SHA, str(out), 'h1', 'h2'], env=env,
                          capture_output=True, text=True, timeout=60)
    return done, out


def test_the_script_parses():
    subprocess.run(['bash', '-n', str(SCRIPT)], check=True)


def test_a_hanging_client_is_abandoned_once_the_labelled_image_exists(tmp_path):
    done, out = run(tmp_path, 'touch STATE; sleep 20')
    assert done.returncode == 0, done.stderr
    lock = json.loads(out.read_text())
    assert lock['development'] is True and lock['components']['honua-server']['artifacts'][0]['digest'] == ID


def test_a_client_that_fails_before_the_image_appears_fails(tmp_path):
    done, _ = run(tmp_path, 'exit 3')
    assert done.returncode != 0 and 'before the image appeared' in done.stderr


def test_the_build_times_out(tmp_path):
    done, _ = run(tmp_path, 'sleep 20', timeout='2')
    assert done.returncode != 0 and 'did not produce' in done.stderr
