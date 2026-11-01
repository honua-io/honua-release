"""Exact reviewed client selection; no latest-version or certification fallback."""
import copy
import re

SDK_FAMILY = ('honua-sdk-js', 'honua-sdk', 'honua-react',
              'honua-geometry', 'honua-app-platform')
CLIENTS = (*SDK_FAMILY, 'honua-mcp-server', 'create-honua-app')
PIN_FIELDS = ('version', 'sourceSha', 'integrity', 'filename', 'publicationState')


def stable_version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError(f'Expected an exact stable version, got {value!r}')
    return value


def selected_versions(reviewed, sdk_override=None):
    artifacts = reviewed['clientArtifacts']
    versions = {key: stable_version(artifacts[key]['version']) for key in CLIENTS}
    sdk = artifacts['honua-sdk-js']
    for key in SDK_FAMILY:
        if versions[key] != sdk['version'] or artifacts[key]['sourceSha'] != sdk['sourceSha']:
            raise ValueError(f'{key}: reviewed SDK family has inconsistent version/source')
    component = reviewed['components']['honua-sdk-js']
    if component['version'] != sdk['version'] or component['sha'] != sdk['sourceSha']:
        raise ValueError('Reviewed SDK component differs from its artifact')
    if reviewed['evidenceSources']['sdk']['producerSha'] != sdk['sourceSha']:
        raise ValueError('Reviewed SDK evidence producer differs from its artifact')
    if sdk_override is not None:
        version = stable_version(sdk_override)
        versions.update({key: version for key in SDK_FAMILY})
    return versions


def validate_candidate(candidate, reviewed, sdk_override=None):
    """Require exact reviewed identity, except explicit temporary SDK preparation.

    Override callers must independently verify the stable release tag, registry
    bytes, signed provenance and each changed pin before emitting any candidate.
    The runtime deliberately never supplies this override.
    """
    versions = selected_versions(reviewed, sdk_override)
    expected = copy.deepcopy(reviewed)
    if sdk_override is not None:
        sdk = candidate['clientArtifacts']['honua-sdk-js']
        for key in SDK_FAMILY:
            pin = candidate['clientArtifacts'][key]
            if (pin['version'] != versions[key] or pin['sourceSha'] != sdk['sourceSha']
                    or pin['publicationState'] != 'published'):
                raise ValueError(f'{key}: temporary SDK family is not coherent and published')
            for field in PIN_FIELDS:
                expected['clientArtifacts'][key][field] = pin[field]
        expected['components']['honua-sdk-js'].update(version=sdk['version'], sha=sdk['sourceSha'])
        expected['evidenceSources']['sdk']['producerSha'] = sdk['sourceSha']
    if candidate != expected:
        raise ValueError('Candidate differs from reviewed pins outside the explicit temporary SDK selection')
