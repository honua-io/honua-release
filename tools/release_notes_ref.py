#!/usr/bin/env python3
"""Generate nightly notes and retain post-gate documents in an immutable Git parent.

The bundle is imported before publishing the lock, whose commit must name this commit as
a parent. No tag or branch is moved here. The resulting repo@rev:path#sha256 references
become reachable when the immutable nightly-lock tag is pushed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

from finalize_release import render_release_notes
from release_facts import notes_reference, source_reference


def snapshot(manifest: Path, matrix: Path, report: dict, output: Path,
             documents: dict[str, bytes] | None = None) -> tuple[str, dict[str, str]]:
    repository = 'https://github.com/' + report['candidate']['source']['repository']
    label = report['platform_label']
    notes_path = f'release-notes/{label}.md'
    files = dict(documents or {})
    if notes_path in files:
        raise ValueError('release notes cannot be supplied by the caller')
    notes = render_release_notes(yaml.safe_load(manifest.read_text()),
                                yaml.safe_load(matrix.read_text()), label, report,
                                report['candidate']['train']['runUrl'])
    files[notes_path] = notes.encode('utf-8')
    # Validate paths before writing into the temporary repository.
    for path, data in files.items():
        source_reference({'repository': repository, 'revision': 'a' * 40, 'path': path})
        if not isinstance(data, bytes) or not data:
            raise ValueError(f'{path}: generated document must contain bytes')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nightly-notes-') as directory:
        root = Path(directory)
        def git(*args):
            return subprocess.run(['git', *args], cwd=root, check=True,
                                  capture_output=True, env=env).stdout.decode().strip()
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'Mike McDougall', 'GIT_AUTHOR_EMAIL': 'mike@honua.io',
               'GIT_COMMITTER_NAME': 'Mike McDougall', 'GIT_COMMITTER_EMAIL': 'mike@honua.io',
               'GIT_AUTHOR_DATE': report['generatedAt'], 'GIT_COMMITTER_DATE': report['generatedAt']}
        git('init', '-q')
        for path, data in files.items():
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        git('add', '--', *files)
        git('commit', '-qm', f'chore: retain nightly evidence for {label}')
        revision = git('rev-parse', 'HEAD')
        git('bundle', 'create', str((output / 'release-notes.bundle').resolve()), 'HEAD')
    references = {path: notes_reference({'repository': repository, 'revision': revision,
                                        'path': path, 'sha256': 'sha256:' + hashlib.sha256(data).hexdigest()})
                  for path, data in files.items()}
    (output / 'release-notes-revision.txt').write_text(revision + '\n')
    (output / 'release-notes-ref.txt').write_text(references[notes_path] + '\n')
    return revision, references


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--matrix', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--out-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    snapshot(args.manifest, args.matrix, json.loads(args.report.read_text()), args.out_dir)
    print((args.out_dir / 'release-notes-ref.txt').read_text().strip())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
