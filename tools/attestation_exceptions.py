"""Founder-ruled, dated exceptions for publisher attestations on exact pinned bytes.

Ruling `sdk-attestation-exception-2026-10-27` (docs/2026.1-release-decision-overrides.json)
lets 2026.1 ship six already-published client components whose bytes predate their
publishers' attestation workflows. Sigstore attestations cannot be added after the fact
to bytes a workflow did not build, and re-releasing them would invalidate the
certification evidence gathered against these pins.

The exception is deliberately narrow:

* it names exact artifacts (component, kind, coordinate, version) and the exact digest
  the lock records for them. Re-pinning a component to any other bytes, a newer version
  included, leaves it outside the exception and therefore requires attestations;
* it only excuses an attestation that is *absent*. A present attestation that fails
  verification is still a refusal;
* server images (OCI images and charts, and any honua-server artifact) can never be
  excepted: their publisher attestations stay mandatory;
* every entry carries an owner, a reason and an expiry (the component's first attested
  release), which the post-gate report and the release notes disclose.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DECISION_OVERRIDES = ROOT / 'docs/2026.1-release-decision-overrides.json'
RULING = 'sdk-attestation-exception-2026-10-27'
EXCLUDED_KINDS = frozenset({'image', 'oci-chart'})
EXCLUDED_COMPONENTS = frozenset({'honua-server'})
DIGEST = re.compile(r'(?:sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128})\Z')
REQUIRED = ('component', 'kind', 'coordinate', 'version', 'digest', 'owner', 'reason', 'expires', 'target')
Key = tuple[str, str, str, str, str]


def _key(component: str, artifact: dict, digest: str) -> Key:
    return (component, str(artifact.get('kind', '')), str(artifact.get('coordinate', '')),
            str(artifact.get('version', '')), digest)


def load(path: Path | None = None) -> dict[Key, dict]:
    """Return the ruled exceptions keyed by exact artifact identity and digest; refuse a malformed ruling."""
    path = DECISION_OVERRIDES if path is None else path
    try:
        rulings = json.loads(path.read_text(encoding='utf-8')).get('rulings') or {}
    except (OSError, ValueError) as exc:
        raise ValueError(f'{path}: unreadable decision overrides: {exc}') from exc
    ruling = rulings.get(RULING)
    if ruling is None:
        return {}
    entries = ruling.get('artifacts')
    if not isinstance(entries, list) or not entries:
        raise ValueError(f'{RULING}: the ruling must list its excepted artifacts')
    exceptions: dict[Key, dict] = {}
    for index, entry in enumerate(entries):
        context = f'{RULING}.artifacts[{index}]'
        if not isinstance(entry, dict):
            raise ValueError(f'{context}: must be an object')
        missing = [field for field in REQUIRED if not isinstance(entry.get(field), str) or not entry[field].strip()]
        if missing:
            raise ValueError(f'{context}: missing {", ".join(missing)}')
        if entry['kind'] in EXCLUDED_KINDS or entry['component'] in EXCLUDED_COMPONENTS:
            raise ValueError(f'{context}: server images and OCI artifacts always require attestations')
        if not DIGEST.fullmatch(entry['digest']):
            raise ValueError(f'{context}: digest must be the exact sha256:/sha512: lock digest')
        key = (entry['component'], entry['kind'], entry['coordinate'], entry['version'], entry['digest'])
        if key in exceptions:
            raise ValueError(f'{context}: duplicate exception')
        exceptions[key] = {**{field: entry[field] for field in REQUIRED}, 'ruling': RULING}
    return exceptions


def lock_digest(artifact: dict) -> str:
    """The digest the lock binds an artifact to (npm integrity is sha512)."""
    if artifact.get('integrity'):
        return 'sha512:' + base64.b64decode(str(artifact['integrity']).removeprefix('sha512-'), validate=True).hex()
    return str(artifact.get('digest') or artifact.get('sha256') or '')


def excepted_components(lock_components: dict, exceptions: dict[Key, dict]) -> set[str]:
    """Components whose every published artifact is an exactly ruled exception."""
    return {name for name, entry in lock_components.items()
            if entry.get('artifacts') and all(applies(exceptions, name, artifact, lock_digest(artifact))
                                              for artifact in entry['artifacts'])}


def applies(exceptions: dict[Key, dict], component: str, artifact: dict, digest: str) -> dict | None:
    """The ruled exception for exactly these pinned bytes, or None."""
    if artifact.get('kind') in EXCLUDED_KINDS or component in EXCLUDED_COMPONENTS:
        return None
    return exceptions.get(_key(component, artifact, digest))
