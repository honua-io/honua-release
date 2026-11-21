"""A component published as a static bundle: a GitHub Actions artifact of a trunk push run.

honua-console is a static SPA (founder ruling, 9 Nov): its trunk pushes run the publisher workflow,
which uploads the artifact (the bundle zip plus its `<asset>.json` receipt) and attests the bundle
zip itself. A trunk commit names a bundle only through a successful push run of that workflow at
that commit, and the lock binds the inner zip's sha256, the subject the publisher attested.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import zipfile

PREFIX = 'actions-artifact:'
KIND = 'static-bundle'
COORDINATE = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/artifacts/([A-Za-z0-9_.-]+)')


class Unavailable(ValueError):
    """This commit has no usable bundle; the trunk walk skips it with this reason."""


def artifact_name(component: dict) -> str | None:
    value = str(component.get('artifact') or '')
    return value.removeprefix(PREFIX) if value.startswith(PREFIX) else None


def coordinate(repository: str, name: str) -> str:
    return f'https://github.com/{repository}/actions/artifacts/{name}'


def publisher_workflow(component: dict, repository: str) -> str:
    """The workflow file that uploads and attests the bundle (the lock's signer workflow)."""
    from mint_nightly_lock import PUBLISHER_WORKFLOWS
    declared = str(component.get('attestationWorkflow') or '')
    workflow = declared.rsplit('/', 1)[-1] if declared else PUBLISHER_WORKFLOWS.get(repository)
    if not workflow:
        raise ValueError(f'{repository}: no publisher workflow for its static bundle')
    return workflow


def find(github, repository: str, workflow: str, name: str, sha: str) -> dict:
    """The unexpired named artifact of the newest successful push run of `workflow` at `sha`."""
    runs = github.json(f'repos/{repository}/actions/runs?head_sha={sha}&event=push&per_page=100')
    for run in sorted(runs.get('workflow_runs') or [], key=lambda row: row.get('id') or 0, reverse=True):
        if (run.get('path') != f'.github/workflows/{workflow}' or run.get('head_sha') != sha
                or run.get('event') != 'push' or run.get('conclusion') != 'success'):
            continue
        listed = github.json(f"repos/{repository}/actions/runs/{run['id']}/artifacts?name={name}")
        for artifact in listed.get('artifacts') or []:
            if artifact.get('name') == name and not artifact.get('expired'):
                return artifact
    raise Unavailable(f'no {name} artifact from a successful {workflow} push run')


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


def identity(github, repository: str, artifact: dict, sha: str) -> dict:
    """The manifest identity of an attested bundle at `sha`, from its own receipt."""
    from resolve_trunk_candidate import ResolutionError
    receipt, _ = bundle(github, repository, artifact)
    digest = 'sha256:' + receipt['sha256']
    try:
        attested = github.json(f'repos/{repository}/attestations/{digest}').get('attestations')
    except ResolutionError as exc:
        if getattr(exc, 'status', None) != 404:
            raise
        attested = None
    if not attested:
        raise Unavailable(f'bundle {digest} has no publisher attestation')
    return {'artifactSha256': digest, 'artifactSourceRevision': sha,
            'artifactVersion': str(receipt.get('version'))}


def select(github, repository: str, workflow: str, name: str, sha: str) -> dict:
    return identity(github, repository, find(github, repository, workflow, name, sha), sha)


def published_bytes(artifact: dict, github) -> bytes:
    """Mint's reader: the bundle bytes the lock row names, from its publisher's push run."""
    match = COORDINATE.fullmatch(str(artifact.get('coordinate') or ''))
    if not match:
        raise ValueError(f"no static-bundle reader for {artifact.get('coordinate')}")
    repository, name = match.groups()
    workflow = publisher_workflow({}, repository)
    try:
        found = find(github, repository, workflow, name, str(artifact.get('sourceRevision') or ''))
        return bundle(github, repository, found)[1]
    except Unavailable as exc:
        raise ValueError(f'{repository}: {exc}') from exc


def gh_bytes(path: str) -> bytes:
    """Authenticated binary `gh api` read (artifact downloads redirect to blob storage)."""
    return subprocess.run(['gh', 'api', path], capture_output=True, check=True).stdout
