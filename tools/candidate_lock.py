#!/usr/bin/env python3
"""Whether a frozen platform lock is the candidate that platform-manifest.yaml pins.

The lock must hash to the digest it was dispatched with, name these exact manifest bytes as its input, and
carry exactly one image per pinned component whose repository, digest and source revision are the pin.

Usage: python3 tools/candidate_lock.py --lock platform-lock.json --digest sha256:<hex> \\
           --manifest platform-manifest.yaml [--component server ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA = re.compile(r"[0-9a-f]{40}")


def resolve_pins(manifest: dict, components: tuple[str, ...]) -> dict:
    """{component: {image, sha, digest}} for each `honua-<component>` pin, refusing a mutable or foreign one."""
    result = {}
    for component in components:
        pin = manifest['components'][f'honua-{component}']
        digest, sha = pin.get('digest', ''), pin.get('sha', '')
        if not _DIGEST.fullmatch(digest) or not _SHA.fullmatch(sha):
            raise ValueError(f'{component} requires an immutable digest and source SHA')
        image = pin.get('image', '')
        prefix = rf'ghcr\.io/honua-io/honua-{component}(?::[A-Za-z0-9_.-]+)?'
        if not re.fullmatch(prefix + r'(?:@sha256:[0-9a-f]{64})?', image):
            raise ValueError(f'{component} must use its own GHCR repository')
        if '@' in image and image.split('@')[1] != digest:
            raise ValueError(f'{component} image disagrees with the manifest digest')
        result[component] = {'image': image.split('@')[0] + '@' + digest, 'sha': sha, 'digest': digest}
    return result


def validate_lock(raw: bytes, expected_digest: str, manifest: bytes, pins: dict) -> None:
    """Raise ValueError unless the lock bytes are the dispatched lock of this manifest and its pins."""
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError('platform lock bytes do not match the supplied digest')
    lock = json.loads(raw)
    if lock.get('sourceInputs', {}).get('platformManifest', {}).get('sha256') != 'sha256:' + hashlib.sha256(manifest).hexdigest():
        raise ValueError('platform lock does not bind this manifest')
    for name, pin in pins.items():
        artifacts = lock.get('components', {}).get(f'honua-{name}', {}).get('artifacts', [])
        images = [item for item in artifacts if isinstance(item, dict) and item.get('kind') == 'image']
        if len(images) != 1:
            raise ValueError(f'platform lock must contain exactly one {name} image')
        image = images[0]
        repository = pin['image'].split('@')[0].rsplit(':', 1)[0]
        if (image.get('coordinate', '').removeprefix('oci://') != repository or
                image.get('digest') != pin['digest'] or image.get('sourceRevision') != pin['sha']):
            raise ValueError(f'platform lock {name} image differs from manifest')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument('--lock', required=True)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--manifest', default='platform-manifest.yaml')
    parser.add_argument('--component', action='append', default=None,
                        help='pinned component to check, without the honua- prefix (repeatable; default server)')
    args = parser.parse_args(argv)
    manifest = Path(args.manifest).read_bytes()
    try:
        pins = resolve_pins(yaml.safe_load(manifest), tuple(args.component or ('server',)))
        validate_lock(Path(args.lock).read_bytes(), args.digest, manifest, pins)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f'candidate lock refused: {exc}', file=sys.stderr)
        return 1
    print(f'candidate lock {args.digest} binds the manifest pins {sorted(pins)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
