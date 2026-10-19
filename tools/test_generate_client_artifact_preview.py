import copy
import json

import pytest
import yaml

import generate_client_artifact_preview as preview


class GitHub:
    def __init__(self, green=True):
        self.allow = green

    def green(self, *args):
        return self.allow, 'controlled source CI verdict'


def context(monkeypatch):
    artifact = {'ecosystem': 'nuget', 'registry': 'nuget.org', 'package': 'Honua.Sdk',
                'repository': 'honua-io/honua-sdk-dotnet', 'version': '1.0.0', 'sourceSha': 'a' * 40,
                'filename': 'honua.sdk.1.0.0.nupkg', 'digest': 'sha256:' + '1' * 64,
                'publicationState': 'published', 'targets': ['net10.0']}
    manifest = {'components': {'honua-server': {'sha': 'f' * 40, 'digest': 'sha256:' + 'f' * 64},
                'honua-sdk-dotnet': {'artifact': 'nuget:Honua.Sdk', 'repository': 'https://github.com/honua-io/honua-sdk-dotnet',
                                     'version': '1.0.0', 'sha': 'a' * 40}},
                'protocolCertification': {'ledger': {'status': 'pending', 'commit': 'e' * 40}},
                'clientArtifacts': {'sdk': artifact}}
    raw = yaml.safe_dump(manifest, sort_keys=False).encode()
    newer = {**artifact, 'version': '1.1.1', 'sourceSha': 'b' * 40,
             'filename': 'honua.sdk.1.1.1.nupkg', 'digest': 'sha256:' + '2' * 64}
    item = {'schema': 'honua.client-artifact-adoption-proposal/v1', 'state': 'review-required',
            'candidateQualification': 'pending', 'client': 'sdk', 'manifestSha256': preview.digest(raw),
            'previousArtifact': copy.deepcopy(artifact), 'proposedArtifact': newer,
            'verifiedIdentity': {'version': '1.1.1', 'sourceRevision': 'b' * 40, 'sha256': newer['digest']}}
    monkeypatch.setattr(preview.proposal, 'propose', lambda *args: copy.deepcopy(item))
    monkeypatch.setattr(preview.resolver, 'component_versions', lambda *args: {'contractVersions': {'sdk-certification': 'v1'}})
    return raw, item


def test_projection_uses_normal_published_selection_and_preserves_bindings(monkeypatch):
    raw, item = context(monkeypatch)
    projected, candidate = preview.project_manifest(raw, [item], GitHub())
    assert yaml.safe_load(projected) == candidate
    assert candidate['components']['honua-sdk-dotnet']['sha'] == 'b' * 40
    assert candidate['components']['honua-sdk-dotnet']['artifactSha256'] == 'sha256:' + '2' * 64
    original = yaml.safe_load(raw)
    assert candidate['components']['honua-server'] == original['components']['honua-server']
    assert candidate['protocolCertification'] == original['protocolCertification']
    assert original['clientArtifacts']['sdk']['version'] == '1.0.0'


@pytest.mark.parametrize('field,value', [('manifestSha256', 'sha256:' + '0' * 64),
    ('state', 'approved'), ('candidateQualification', 'pass'), ('schema', 'unknown')])
def test_forged_proposal_context_is_refused(monkeypatch, field, value):
    raw, item = context(monkeypatch)
    item[field] = value
    with pytest.raises(preview.PreviewError, match='schema/state/manifest'):
        preview.project_manifest(raw, [item], GitHub())


def test_previous_identity_and_registry_drift_are_refused(monkeypatch):
    raw, item = context(monkeypatch)
    item['previousArtifact']['sourceSha'] = 'c' * 40
    with pytest.raises(preview.PreviewError, match='previous artifact'):
        preview.project_manifest(raw, [item], GitHub())
    raw, item = context(monkeypatch)
    altered = copy.deepcopy(item)
    altered['proposedArtifact']['sourceSha'] = 'c' * 40
    monkeypatch.setattr(preview.proposal, 'propose', lambda *args: altered)
    with pytest.raises(preview.PreviewError, match='live verified registry'):
        preview.project_manifest(raw, [item], GitHub())


def test_red_source_is_never_selected_as_green(monkeypatch):
    raw, item = context(monkeypatch)
    with pytest.raises(preview.resolver.ResolutionError, match='CI controlled source CI verdict'):
        preview.project_manifest(raw, [item], GitHub(False))


def test_duplicate_and_repository_substitution_are_refused(monkeypatch):
    raw, item = context(monkeypatch)
    with pytest.raises(preview.PreviewError, match='duplicate client'):
        preview.project_manifest(raw, [item, item], GitHub())
    item['proposedArtifact']['repository'] = 'attacker/sdk'
    with pytest.raises(preview.resolver.ResolutionError, match='repository does not match'):
        preview.project_manifest(raw, [item], GitHub())


def row(operation='Query', version='1.0.0', facets=None):
    return {'capability_key': 'sdk-dotnet.features', 'surface': 'features', 'operation': operation,
            'canonical_client': 'Honua SDK .NET', 'client_lane': 'sdk-dotnet',
            'deployment_target': 'local-docker', 'client_version': version,
            'scenario_facets': facets or ['positive', 'negative'], 'required_tier': 'nightly'}


def test_identity_diff_catches_loss_even_when_total_count_grows():
    result = preview.requirement_diff({'requirements': [row('Original')]},
        {'requirements': [row('Added1'), row('Added2')]})
    assert len(result['removedObligations']) == 1
    assert len(result['addedUnqualifiedObligations']) == 2
    assert result['qualification'].startswith('pending')


def test_identity_diff_retains_semantic_and_version_changes():
    result = preview.requirement_diff({'requirements': [row()]},
        {'requirements': [row(version='1.1.1', facets=['positive'])]})
    assert not result['removedObligations']
    assert len(result['changedObligations']) == 1
    assert result['changedObligations'][0]['before']['scenario_facets'] == ['positive', 'negative']


def test_tier_exclusions_remain_explicit_even_when_catalog_row_tier_is_unchanged(tmp_path):
    path = 'certification/sources/sdk-dotnet/sdk-certification.v1.json'
    old = {'operations': [{'id': 'Route', 'requiredTiers': ['nightly', 'release']}]}
    new = {'operations': [{'id': 'Route', 'requiredTiers': ['nightly'], 'releaseDenominator': 'excluded'},
                          {'id': 'Metadata', 'requiredTiers': ['pr', 'nightly', 'release']}]}
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(old))
    result = preview.source_tier_diff(tmp_path, {path: json.dumps(new).encode()})
    assert {item['operation'] for item in result} == {'Route', 'Metadata'}
    assert all(item['qualification'] == 'pending' for item in result)


def test_generated_preview_is_isolated_and_requires_actual_review(monkeypatch, tmp_path):
    raw, item = context(monkeypatch)
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'platform-manifest.yaml').write_bytes(raw)
    (root / 'customer-install-manifest.json').write_text('{"historical":true}')
    path = root / preview.convergence.CATALOG
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'requirements': [row()]}))
    def prepare(trial, github, tier):
        assert yaml.safe_load((trial / 'platform-manifest.yaml').read_bytes())['clientArtifacts']['sdk']['version'] == '1.1.1'
        return {'mode': 'plan'}, {str(preview.convergence.CATALOG): json.dumps(
            {'requirements': [row(version='1.1.1'), row('NewOperation')]}).encode()}, 'old-ledger'
    monkeypatch.setattr(preview.convergence, 'prepare', prepare)
    output = tmp_path / 'preview'
    report = preview.generate(root, output, [item], GitHub(), object())
    assert report['state'] == 'review-required' and report['candidateAdmission'] is False
    assert (root / 'platform-manifest.yaml').read_bytes() == raw
    assert (root / 'customer-install-manifest.json').read_text() == '{"historical":true}'
    assert (output / 'preview-inventory.json').is_file()
    assert len(report['requirements']['addedUnqualifiedObligations']) == 1


def test_catalog_loss_refuses_output_and_keeps_original(monkeypatch, tmp_path):
    raw, item = context(monkeypatch)
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'platform-manifest.yaml').write_bytes(raw)
    (root / 'customer-install-manifest.json').write_text('{}')
    path = root / preview.convergence.CATALOG
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'requirements': [row('Original')]}))
    monkeypatch.setattr(preview.convergence, 'prepare', lambda *args: ({'mode': 'plan'},
        {str(preview.convergence.CATALOG): json.dumps({'requirements': [row('Other1'), row('Other2')]}).encode()}, 'ledger'))
    output = tmp_path / 'preview'
    with pytest.raises(preview.PreviewError, match='loses original obligations'):
        preview.generate(root, output, [item], GitHub(), object())
    assert not output.exists() and (root / 'platform-manifest.yaml').read_bytes() == raw


def test_source_or_existing_output_cannot_be_overwritten(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    for output in (root, root / 'new', tmp_path):
        with pytest.raises(preview.PreviewError, match='nonexistent and outside'):
            preview.generate(root, output, [])


def test_normal_generator_refusal_preserves_diagnostic_and_creates_no_output(monkeypatch, tmp_path):
    import subprocess
    raw, item = context(monkeypatch)
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'platform-manifest.yaml').write_bytes(raw)
    (root / 'customer-install-manifest.json').write_text('{}')
    def refuse(*args):
        raise subprocess.CalledProcessError(1, ['normal-catalog-generator'], output='source summary',
                                            stderr='required source operation is missing')
    monkeypatch.setattr(preview.convergence, 'prepare', refuse)
    output = tmp_path / 'preview'
    with pytest.raises(preview.PreviewError, match='required source operation is missing'):
        preview.generate(root, output, [item], GitHub(), object())
    assert not output.exists() and (root / 'platform-manifest.yaml').read_bytes() == raw
