"""honua-console ships a static bundle: the night binds the attested trunk CI bundle, not an image."""

import hashlib
import io
import json
import zipfile

import pytest

import generate_platform_lock as generator
import mint_nightly_lock as mint
import platform_version
import resolve_trunk_candidate as resolver
import static_bundle
import validate_platform_lock as validator
from test_platform_lock import REVISION, assert_refused, draft_of, evidence_manifest, valid_lock

REPOSITORY = 'honua-io/honua-console'
NEWEST, BUNDLED = '1' * 40, '2' * 40
WORKFLOW = '.github/workflows/static-bundle.yml'
INNER = b'console bundle bytes'
INNER_SHA = 'sha256:' + hashlib.sha256(INNER).hexdigest()


def console(**overrides):
    row = {'repository': f'https://github.com/{REPOSITORY}', 'lifecycleStatus': 'GA',
           'version': 'pre-release', 'artifact': 'actions-artifact:console-static-release'}
    row.update(overrides)
    return row


def artifact_zip(sha, *, inner=INNER, receipt_sha=None):
    version = '0.0.0-trunk.' + sha[:12]
    asset = f'honua-console-{version}.zip'
    receipt = {'asset': asset, 'version': version,
               'sha256': receipt_sha or hashlib.sha256(inner).hexdigest()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(asset, inner)
        archive.writestr(asset + '.json', json.dumps(receipt))
    return buffer.getvalue()


class BundleGitHub:
    """Trunk is NEWEST then BUNDLED; only BUNDLED has a static-bundle push run with the artifact."""

    def __init__(self, *, attested=True, payload=None, expired=False, workflow=WORKFLOW):
        self.attested, self.expired, self.workflow = attested, expired, workflow
        self.payload = payload if payload is not None else artifact_zip(BUNDLED)
        self.downloads = []

    def commits(self, repository, limit):
        yield from [NEWEST, BUNDLED][:limit]

    def green(self, name, repository, sha):
        return True, 'full CI green'

    def json(self, path):
        if path.startswith(f'repos/{REPOSITORY}/actions/runs?head_sha='):
            sha = path.split('head_sha=', 1)[1].split('&', 1)[0]
            runs = [{'id': 7, 'path': self.workflow, 'head_sha': sha, 'event': 'push',
                     'conclusion': 'success'}] if sha == BUNDLED else []
            return {'total_count': len(runs), 'workflow_runs': runs}
        if path.startswith(f'repos/{REPOSITORY}/actions/runs/7/artifacts'):
            digest = 'sha256:' + hashlib.sha256(self.payload).hexdigest()
            return {'total_count': 1, 'artifacts': [
                {'id': 99, 'name': 'console-static-release', 'expired': self.expired, 'digest': digest}]}
        if path == f'repos/{REPOSITORY}/attestations/{INNER_SHA}':
            if not self.attested:
                raise resolver.ResolutionError('gh api failed: HTTP 404', status=404)
            return {'attestations': [{'bundle': {}}]}
        raise AssertionError(path)

    def download(self, path):
        assert path == f'repos/{REPOSITORY}/actions/artifacts/99/zip'
        self.downloads.append(path)
        return self.payload


def select(github):
    walk = {}
    return resolver.select_component('honua-console', console(), github, None, 2, walk), walk


def test_the_night_binds_the_newest_green_attested_trunk_bundle():
    github = BundleGitHub()
    selected, walk = select(github)
    assert selected['sha'] == BUNDLED
    assert selected['artifactSourceRevision'] == BUNDLED
    assert selected['artifactSha256'] == INNER_SHA
    assert selected['artifactVersion'] == '0.0.0-trunk.' + BUNDLED[:12]
    assert 'image' not in selected and 'digest' not in selected
    assert walk['skipped'] == [{'sha': NEWEST, 'reason':
                                'no console-static-release artifact from a successful static-bundle.yml push run'}]


@pytest.mark.parametrize('github,reason', [
    (BundleGitHub(attested=False), 'no publisher attestation'),
    (BundleGitHub(expired=True), 'no console-static-release artifact'),
    (BundleGitHub(workflow='.github/workflows/ci.yml'), 'no console-static-release artifact'),
    (BundleGitHub(payload=artifact_zip(BUNDLED, receipt_sha='0' * 64)), 'does not match its receipt'),
])
def test_a_bundle_that_is_absent_unattested_or_altered_is_skipped(github, reason):
    with pytest.raises(resolver.ResolutionError, match=f'no qualifying trunk commit.*{reason}'):
        select(github)


def test_an_artifact_whose_download_is_not_its_listed_digest_is_skipped():
    github = BundleGitHub()
    listed = github.json
    github.json = lambda path: ({**listed(path), 'artifacts': [
        {**listed(path)['artifacts'][0], 'digest': 'sha256:' + '0' * 64}]}
        if '/artifacts' in path and '/runs/7/' in path else listed(path))
    with pytest.raises(resolver.ResolutionError, match='download is not the listed artifact digest'):
        select(github)


def test_console_takes_its_bundle_version_not_the_platform_version():
    assert 'honua-console' not in platform_version.IMAGED_COMPONENTS


def test_the_lock_seeds_a_static_bundle_artifact(tmp_path):
    manifest = evidence_manifest()
    manifest['components']['honua-console'] = console(
        sha=REVISION, artifactSourceRevision=REVISION, artifactSha256=INNER_SHA,
        artifactVersion='0.0.0-trunk.' + REVISION[:12], contractVersions={'admin': 'v1'},
        schemaVersions={'diagnostic-bundle': '1.0'})
    draft = draft_of(tmp_path, manifest)
    assert not [item for item in draft.unresolved if 'components.honua-console' in item], draft.unresolved
    assert draft.lock['components']['honua-console']['artifacts'] == [{
        'kind': 'static-bundle', 'coordinate': f'https://github.com/{REPOSITORY}/actions/artifacts/console-static-release',
        'version': '0.0.0-trunk.' + REVISION[:12], 'sourceRevision': REVISION, 'sha256': INNER_SHA}]


def test_a_static_bundle_lock_row_needs_its_sha256():
    lock = valid_lock()
    lock['components']['sdk']['artifacts'] = [{
        'kind': 'static-bundle', 'coordinate': f'https://github.com/{REPOSITORY}/actions/artifacts/console-static-release',
        'version': '0.0.0-trunk.' + REVISION[:12], 'sourceRevision': REVISION}]
    assert_refused(lock, 'static-bundle artifacts require a sha256 hash')
    lock['components']['sdk']['artifacts'][0]['sha256'] = INNER_SHA
    assert validator.validate(lock).ok


def test_mint_reads_the_static_bundle_bytes_from_the_publisher_run():
    github = BundleGitHub()
    artifact = {'kind': 'static-bundle', 'sourceRevision': BUNDLED, 'sha256': INNER_SHA,
                'coordinate': f'https://github.com/{REPOSITORY}/actions/artifacts/console-static-release'}
    assert mint.published_artifact_bytes(artifact, github=github) == INNER
    with pytest.raises(ValueError, match='does not match its locked hash'):
        mint.published_artifact_bytes({**artifact, 'sha256': 'sha256:' + '0' * 64}, github=github)


def test_the_console_publisher_is_the_static_bundle_workflow():
    assert mint.PUBLISHER_WORKFLOWS[REPOSITORY] == 'static-bundle.yml'
    assert static_bundle.publisher_workflow(console(), REPOSITORY) == 'static-bundle.yml'


def test_a_studio_case_runs_the_reviewed_console_image_when_the_manifest_names_a_bundle(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import agent_browser_studio as studio
    from agent_e2e_local_runner import LocalRuntime
    pin = {'image': 'ghcr.io/honua-io/honua-console@sha256:' + 'b' * 64, 'sourceRevision': 'c' * 40}
    (tmp_path / 'pin.json').write_text(json.dumps(pin))
    monkeypatch.setattr(studio, 'CONSOLE_PIN_PATH', tmp_path / 'pin.json')
    (tmp_path / 'platform-manifest.yaml').write_text(json.dumps({'components': {'honua-console': console()}}))
    monkeypatch.setattr(studio, 'CaseConsole', lambda _stack, actual, **_kwargs:
                        SimpleNamespace(url='http://127.0.0.1:1234', image=actual))
    stack = SimpleNamespace(extra={}, compose_project='g41')
    runtime = SimpleNamespace(root=tmp_path, _console_identities={'g41': {'edgeSecret': 'x', 'userId': 'user'}},
                              attest=lambda *_args: None)
    assert LocalRuntime.start_console(runtime, stack) == 'http://127.0.0.1:1234'
    assert stack.extra['_caseConsole'].image == pin['image']
