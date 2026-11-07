import hashlib
import subprocess
from types import SimpleNamespace

import pytest

import validate_platform
from image_platforms import verify_image_tag_digest


@pytest.mark.parametrize('target', ['serving', 'executor'])
@pytest.mark.parametrize('repointed', [False, True])
def test_candidate_rejects_repointed_image_tags(monkeypatch, target, repointed):
    manifests = {'serving': b'{"image":"serving"}', 'executor': b'{"image":"executor"}'}
    coordinate = 'registry.example:5000/server'
    artifacts = {name: {'image': f'{coordinate}:{name}',
                        'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
                 for name, raw in manifests.items()}
    server = {**artifacts['serving'], 'composeExecutor': artifacts['executor']}
    monkeypatch.setattr(validate_platform, 'verify_image_platform_digests', lambda artifact: None)
    calls = []

    def inspect(command, **kwargs):
        calls.append(command)
        reference = command[4]
        name = reference.rsplit(':', 1)[1]
        raw = b'{"image":"replacement"}' if repointed and name == target else manifests[name]
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b'')

    monkeypatch.setattr(subprocess, 'run', inspect)
    findings = validate_platform.Findings()
    validate_platform.check_registry_platform_digests({'components': {'honua-server': server}}, findings)
    if repointed:
        assert any('tag resolves to a different digest' in error for error in findings.errors), findings.errors
    else:
        assert findings.ok, findings.errors
        assert {command[4] for command in calls} == {artifact['image'] for artifact in artifacts.values()}


def test_immutable_image_reference_never_resolves_a_tag(monkeypatch):
    def unexpected_inspection(*args, **kwargs):
        raise AssertionError('an immutable address has no tag to resolve')

    monkeypatch.setattr(subprocess, 'run', unexpected_inspection)
    digest = 'sha256:' + 'a' * 64
    verify_image_tag_digest(f'registry.example:5000/server@{digest}', digest)


@pytest.mark.parametrize('reference', [None, '', 123])
def test_malformed_image_reference_is_a_validation_error(reference):
    with pytest.raises(ValueError, match='registry reference'):
        verify_image_tag_digest(reference, 'sha256:' + 'a' * 64)
