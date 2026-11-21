"""A component published as a static bundle: a GitHub Actions artifact of a trunk push run.

honua-console is a static SPA (founder ruling, 9 Nov): its trunk pushes run the publisher workflow,
which uploads the artifact (the bundle zip plus its `<asset>.json` receipt) and attests the bundle
zip itself. A trunk commit names a bundle only through a successful push run of that workflow at
that commit, and the lock binds the inner zip's sha256, the subject the publisher attested.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import subprocess
from typing import NamedTuple
import zipfile

PREFIX = 'actions-artifact:'
KIND = 'static-bundle'
# The publisher statements mint requires for a non-image artifact.
ATTESTED = frozenset({'provenance', 'sbom'})
NOT_FOUND = 404
COORDINATE = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/artifacts/([A-Za-z0-9_.-]+)')


class Unavailable(ValueError):
    """This commit has no usable bundle; the trunk walk skips it with this reason."""


def artifact_name(component: dict) -> str | None:
    value = str(component.get('artifact') or '')
    return value.removeprefix(PREFIX) if value.startswith(PREFIX) else None


def coordinate(repository: str, name: str) -> str:
    return f'https://github.com/{repository}/actions/artifacts/{name}'


class Publisher(NamedTuple):
    """Where a static bundle comes from: the repository, its publisher workflow file and artifact name."""
    repository: str
    workflow: str
    name: str


def publisher_workflow(component: dict, repository: str) -> str:
    """The workflow file that uploads and attests the bundle (the lock's signer workflow)."""
    from mint_nightly_lock import PUBLISHER_WORKFLOWS
    declared = str(component.get('attestationWorkflow') or '')
    workflow = declared.rsplit('/', 1)[-1] if declared else PUBLISHER_WORKFLOWS.get(repository)
    if not workflow:
        raise ValueError(f'{repository}: no publisher workflow for its static bundle')
    return workflow


def signer_constraints(artifact: dict) -> dict:
    """Mint's extra signer checks for a bundle: attested by a trunk run on a GitHub-hosted runner."""
    return {'sourceRef': 'refs/heads/trunk', 'denySelfHosted': True} if artifact.get('kind') == KIND else {}


def candidates(github, publisher: Publisher, sha: str):
    """Every unexpired named artifact of a successful push run of the publisher at `sha`, newest run first.

    A rerun or a second push run at one commit uploads another artifact; each is a candidate, so
    the resolver and mint can both find the bytes the lock binds."""
    runs = github.json(f'repos/{publisher.repository}/actions/runs?head_sha={sha}&event=push&per_page=100')
    for run in sorted(runs.get('workflow_runs') or [], key=lambda row: row.get('id') or 0, reverse=True):
        if (run.get('path') != f'.github/workflows/{publisher.workflow}' or run.get('head_sha') != sha
                or run.get('event') != 'push' or run.get('conclusion') != 'success'):
            continue
        listed = github.json(f"repos/{publisher.repository}/actions/runs/{run['id']}/artifacts?name={publisher.name}")
        yield from (artifact for artifact in listed.get('artifacts') or []
                    if artifact.get('name') == publisher.name and not artifact.get('expired'))


def unpack(payload: bytes, listed_digest: str | None) -> tuple[dict, bytes]:
    """The receipt and bundle bytes in a downloaded artifact; the bundle must match its receipt."""
    if listed_digest and listed_digest != 'sha256:' + hashlib.sha256(payload).hexdigest():
        raise Unavailable('download is not the listed artifact digest')
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            receipts = [entry for entry in archive.namelist() if entry.endswith('.zip.json')]
            if len(receipts) != 1:
                raise Unavailable(f'artifact holds {len(receipts)} bundle receipts, not one')
            receipt = json.loads(archive.read(receipts[0]))
            inner = archive.read(str(receipt.get('asset')))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise Unavailable(f'artifact is not a bundle and its receipt ({exc})') from exc
    if receipt.get('sha256') != hashlib.sha256(inner).hexdigest():
        raise Unavailable('bundle does not match its receipt sha256')
    return receipt, inner


def bundle(github, repository: str, artifact: dict) -> tuple[dict, bytes]:
    payload = github.download(f"repos/{repository}/actions/artifacts/{artifact['id']}/zip")
    return unpack(payload, artifact.get('digest'))


def attested_fields(github, repository: str, digest: str) -> set[str]:
    """Which publisher statements (provenance, sbom) the repository holds for exactly `digest`.

    Mint verifies each statement's signature and signer; this pre-check keeps the trunk walk from
    binding a bundle mint must refuse."""
    from mint_nightly_lock import statement_field
    from resolve_trunk_candidate import ResolutionError
    try:
        rows = github.json(f'repos/{repository}/attestations/{digest}?per_page=100').get('attestations') or []
    except ResolutionError as exc:
        if getattr(exc, 'status', None) != NOT_FOUND:
            raise
        rows = []
    fields = set()
    for row in rows:
        payload = (((row or {}).get('bundle') or {}).get('dsseEnvelope') or {}).get('payload', '')
        try:
            statement = json.loads(base64.b64decode(payload, validate=True))
        except (ValueError, TypeError):
            continue
        fields.add(statement_field(statement, {digest}))
    return fields - {None}


def identity(github, repository: str, artifact: dict, sha: str) -> dict:
    """The manifest identity of an attested bundle at `sha`, from its own receipt."""
    receipt, _ = bundle(github, repository, artifact)
    version = f'0.0.0-trunk.{sha[:12]}'
    if receipt.get('version') != version:
        raise Unavailable(f"bundle receipt names version {receipt.get('version')!r}, not {version}")
    digest = 'sha256:' + receipt['sha256']
    missing = sorted(ATTESTED - attested_fields(github, repository, digest))
    if missing:
        raise Unavailable(f"bundle {digest} has no publisher {' or '.join(missing)} attestation")
    return {'artifactSha256': digest, 'artifactSourceRevision': sha, 'artifactVersion': version}


def select(github, publisher: Publisher, sha: str) -> dict:
    """The newest usable bundle at `sha`; the first refusal names why none was."""
    reasons = []
    for artifact in candidates(github, publisher, sha):
        try:
            return identity(github, publisher.repository, artifact, sha)
        except Unavailable as exc:
            reasons.append(str(exc))
    raise Unavailable(reasons[0] if reasons else
                      f'no {publisher.name} artifact from a successful {publisher.workflow} push run')


def published_bytes(artifact: dict, github) -> bytes:
    """Mint's reader: the bundle bytes the lock row names, from a push run of its publisher."""
    match = COORDINATE.fullmatch(str(artifact.get('coordinate') or ''))
    if not match:
        raise ValueError(f"no static-bundle reader for {artifact.get('coordinate')}")
    repository, name = match.groups()
    publisher = Publisher(repository, publisher_workflow({}, repository), name)
    expected = str(artifact.get('sha256') or '')
    found = None
    for candidate in candidates(github, publisher, str(artifact.get('sourceRevision') or '')):
        try:
            found = bundle(github, repository, candidate)[1]
        except Unavailable:
            continue
        if 'sha256:' + hashlib.sha256(found).hexdigest() == expected:
            return found
    if found is None:
        raise ValueError(f'{repository}: no readable {name} artifact at {artifact.get("sourceRevision")}')
    return found  # the caller refuses bytes that are not the locked hash


def gh_bytes(path: str) -> bytes:
    """Authenticated binary `gh api` read (artifact downloads redirect to blob storage)."""
    return subprocess.run(['gh', 'api', path], capture_output=True, check=True).stdout
