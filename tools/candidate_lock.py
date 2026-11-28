#!/usr/bin/env python3
"""Whether a frozen platform lock is the candidate that platform-manifest.yaml pins.

The lock must hash to the digest it was dispatched with, name these exact manifest bytes as its input, and
carry exactly one artifact per pinned component whose coordinate, digest and source revision are the pin.

Usage: python3 tools/candidate_lock.py --lock platform-lock.json --digest sha256:<hex> \\
           --manifest platform-manifest.yaml [--component server ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

import static_bundle

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA = re.compile(r"[0-9a-f]{40}")


def _resolve_image_pin(component: str, pin: dict) -> dict:
    digest, sha = pin.get('digest', ''), pin.get('sha', '')
    if not isinstance(digest, str) or not isinstance(sha, str) or not _DIGEST.fullmatch(digest) or not _SHA.fullmatch(sha):
        raise ValueError(f'{component} requires an immutable digest and source SHA')
    image = pin.get('image', '')
    prefix = rf'ghcr\.io/honua-io/honua-{component}(?::[A-Za-z0-9_.-]+)?'
    if not isinstance(image, str) or not re.fullmatch(prefix + r'(?:@sha256:[0-9a-f]{64})?', image):
        raise ValueError(f'{component} must use its own GHCR repository')
    if '@' in image and image.split('@')[1] != digest:
        raise ValueError(f'{component} image disagrees with the manifest digest')
    return {'kind': 'image', 'image': image.split('@')[0] + '@' + digest, 'sha': sha, 'digest': digest}


def _resolve_static_bundle_pin(component: str, pin: dict, artifact_name: str) -> dict:
    if pin.get('image'):
        raise ValueError(f'{component} cannot declare both image and static-bundle')
    repository = f'honua-io/honua-{component}'
    if 'repository' in pin and pin['repository'] != f'https://github.com/{repository}':
        raise ValueError(f'{component} must use its own GitHub repository')
    coordinate = static_bundle.coordinate(repository, artifact_name)
    if not artifact_name or artifact_name in ('.', '..') or not static_bundle.COORDINATE.fullmatch(coordinate):
        raise ValueError(f'{component} requires a valid static-bundle artifact name')
    digest, sha = pin.get('artifactSha256', ''), pin.get('sha', '')
    if not isinstance(digest, str) or not isinstance(sha, str) or not _DIGEST.fullmatch(digest) or not _SHA.fullmatch(sha):
        raise ValueError(f'{component} requires an immutable static-bundle digest and source SHA')
    if 'artifactSourceRevision' in pin and pin['artifactSourceRevision'] != sha:
        raise ValueError(f'{component} artifactSourceRevision disagrees with the manifest SHA')
    version = pin.get('artifactVersion', '')
    if not isinstance(version, str) or not version.strip() or version.strip() != version or version == 'pre-release':
        raise ValueError(f'{component} requires a non-empty static-bundle version')
    return {'kind': static_bundle.KIND, 'coordinate': coordinate, 'sha': sha, 'digest': digest, 'version': version}


def resolve_pins(manifest: dict, components: tuple[str, ...]) -> dict:
    """Resolve each `honua-<component>` pin, refusing missing, mutable or foreign coordinates."""
    result = {}
    for component in components:
        pin = manifest['components'][f'honua-{component}']
        if not isinstance(pin, dict):
            raise ValueError(f'{component} pin must be a mapping')
        bundle_name = static_bundle.artifact_name(pin)
        if bundle_name is not None:
            result[component] = _resolve_static_bundle_pin(component, pin, bundle_name)
        else:
            result[component] = _resolve_image_pin(component, pin)
    return result


@dataclass(frozen=True)
class PinnedCandidate:
    """The exact platform-manifest.yaml bytes and the component pins resolved from them."""
    manifest: bytes
    pins: dict


def _validate_image_artifact(name: str, pin: dict, artifacts: list) -> None:
    images = [item for item in artifacts if isinstance(item, dict) and item.get('kind') == 'image']
    if len(images) != 1:
        raise ValueError(f'platform lock must contain exactly one {name} image')
    image = images[0]
    repository = pin['image'].split('@')[0].rsplit(':', 1)[0]
    if (image.get('coordinate', '').removeprefix('oci://') != repository or
            image.get('digest') != pin['digest'] or image.get('sourceRevision') != pin['sha']):
        raise ValueError(f'platform lock {name} image differs from manifest')


def _validate_static_bundle_artifact(name: str, pin: dict, artifacts: list) -> None:
    bundles = [item for item in artifacts if isinstance(item, dict) and item.get('kind') == static_bundle.KIND]
    if len(bundles) != 1:
        raise ValueError(f'platform lock must contain exactly one {name} static-bundle')
    bundle = bundles[0]
    version_mismatch = 'version' in bundle and bundle.get('version') != pin['version']
    if (bundle.get('coordinate') != pin['coordinate'] or bundle.get('sha256') != pin['digest'] or
            bundle.get('sourceRevision') != pin['sha'] or version_mismatch):
        raise ValueError(f'platform lock {name} static-bundle differs from manifest')


def _validate_component_artifacts(name: str, pin: dict, lock: dict) -> None:
    artifacts = lock.get('components', {}).get(f'honua-{name}', {}).get('artifacts', [])
    if not isinstance(artifacts, list):
        raise ValueError(f'platform lock {name} artifacts must be a list')
    kind = pin.get('kind', 'image')
    if kind == 'image':
        _validate_image_artifact(name, pin, artifacts)
        return
    if kind == static_bundle.KIND:
        _validate_static_bundle_artifact(name, pin, artifacts)
        return
    raise ValueError(f'platform lock does not support {name} artifact kind {kind!r}')


def validate_lock(raw: bytes, expected_digest: str, candidate: PinnedCandidate) -> None:
    """Raise ValueError unless the lock bytes are the dispatched lock of this manifest and its pins."""
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError('platform lock bytes do not match the supplied digest')
    lock = json.loads(raw)
    manifest_digest = 'sha256:' + hashlib.sha256(candidate.manifest).hexdigest()
    if lock.get('sourceInputs', {}).get('platformManifest', {}).get('sha256') != manifest_digest:
        raise ValueError('platform lock does not bind this manifest')
    for name, pin in candidate.pins.items():
        _validate_component_artifacts(name, pin, lock)


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
        validate_lock(Path(args.lock).read_bytes(), args.digest, PinnedCandidate(manifest, pins))
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f'candidate lock refused: {exc}', file=sys.stderr)
        return 1
    print(f'candidate lock {args.digest} binds the manifest pins {sorted(pins)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
