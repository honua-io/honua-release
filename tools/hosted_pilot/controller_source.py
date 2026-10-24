#!/usr/bin/env python3
"""Record and verify the trusted image's immutable source archive/index identity."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def require(value, message):
    if not value:
        raise ValueError(message)


def tree():
    result = subprocess.run(['git', 'write-tree'], capture_output=True)
    require(result.returncode == 0, 'controller source index cannot be verified')
    return result.stdout.decode().strip()


def source_digest():
    result = subprocess.run(['git', 'ls-files', '--stage', '-z'], capture_output=True)
    require(result.returncode == 0 and result.stdout, 'controller tracked source index is unavailable')
    rows = []
    for row in result.stdout.rstrip(b'\0').split(b'\0'):
        header, path = row.split(b'\t', 1)
        mode, blob, stage = header.decode().split()
        path = Path(os.fsdecode(path))
        require(stage == '0' and mode in ('100644', '100755', '120000') and not path.is_absolute() and '..' not in path.parts, 'unsupported controller tracked source entry')
        require(path.is_symlink() == (mode == '120000'), 'controller source entry type changed')
        value = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        require(hashlib.sha1(b'blob ' + str(len(value)).encode() + b'\0' + value).hexdigest() == blob, 'controller source bytes differ from reviewed index')
        require(mode == '120000' or bool(path.stat().st_mode & 0o111) == (mode == '100755'), 'controller source executable mode changed')
        rows.append([str(path), mode, hashlib.sha256(value).hexdigest()])
    return hashlib.sha256(json.dumps(rows, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def verify(receipt, source_sha, tree_id, archive_sha256):
    require(re.fullmatch('[a-f0-9]{40}', source_sha) and re.fullmatch('[a-f0-9]{40}', tree_id) and re.fullmatch('[a-f0-9]{64}', archive_sha256), 'immutable controller source identities required')
    require(receipt == {'schema': 'honua.hosted-controller-source/v1', 'source_sha': source_sha, 'tree_id': tree_id, 'archive_sha256': archive_sha256, 'tracked_files_sha256': source_digest()}, 'controller source receipt identity mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['record', 'verify'])
    for name in ['source-sha', 'tree-id', 'archive-sha256', 'receipt']:
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    receipt = {'schema': 'honua.hosted-controller-source/v1', 'source_sha': args.source_sha, 'tree_id': args.tree_id, 'archive_sha256': args.archive_sha256, 'tracked_files_sha256': source_digest()}
    path = Path(args.receipt)
    if args.action == 'record':
        require(tree() == args.tree_id, 'controller source tree differs from reviewed archive')
    verify(receipt if args.action == 'record' else json.loads(path.read_text()), args.source_sha, args.tree_id, args.archive_sha256)
    if args.action == 'record':
        path.write_text(json.dumps(receipt, sort_keys=True) + '\n')
        path.chmod(0o444)
    print('Trusted controller source identity verified.')


if __name__ == '__main__':
    main()
