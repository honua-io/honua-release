"""ECS-only candidate selection through intrinsic manifest validation."""
import copy

import yaml

import resolve_trunk_candidate as resolver
from test_resolve_trunk_candidate import GitHub, MigrationSource, NEW, _lambda_registry


def test_resolve_ecs_candidate_runs_structure_validation_without_lambda(monkeypatch):
    manifest = yaml.safe_load((resolver.ROOT / 'platform-manifest.yaml').read_text())
    matrix = yaml.safe_load((resolver.ROOT / 'compatibility-matrix.yaml').read_text())
    registry, _, _ = _lambda_registry(NEW, 'attestation-only')
    image = registry.image

    def dual_arch_image(name, component, sha):
        result = image(name, {**component, 'architectures': ['amd64']}, sha)
        result['platformDigests']['arm64'] = 'sha256:' + '8' * 64
        return result

    registry.image = dual_arch_image
    source = MigrationSource()
    source.commits = GitHub().commits
    source.green = GitHub().green
    select_component = resolver.select_component
    monkeypatch.setattr(resolver, 'select_component', lambda name, component, *args, **kwargs:
        select_component(name, component, *args, **kwargs) if name == 'honua-server' else copy.deepcopy(component))
    monkeypatch.setattr(resolver, 'released_archive', lambda component: False)
    monkeypatch.setattr(resolver, 'SDK_COMPONENTS', ())
    monkeypatch.setattr(resolver.sdk_baselines, 'SDK_COMPONENTS', ())
    monkeypatch.setattr(resolver, 'verify_manifest', lambda *args, **kwargs: None)
    monkeypatch.setattr(resolver, 'component_versions', lambda github, name, component:
        {key: copy.deepcopy((manifest['components'].get(name) or manifest['experimental'][name])[key]) for key in ('contractVersions', 'schemaVersions')})
    monkeypatch.setattr(resolver, 'content_digest_declaration', lambda *args: None)
    monkeypatch.setattr(resolver.validate_platform, 'check_legacy_evidence_pin_coherence', lambda *args: None)
    validated = []

    def structure_only(candidate, candidate_matrix, *args, **kwargs):
        findings = resolver.validate_platform.Findings()
        resolver.validate_platform.check_structure(candidate, candidate_matrix, findings)
        validated.append(candidate)
        return findings

    monkeypatch.setattr(resolver.validate_platform, 'validate', structure_only)
    candidate, _ = resolver.resolve(manifest, matrix, source, registry, limit=1, protocol_ledger='produce')
    assert validated == [candidate]
    assert candidate['components']['honua-server']['sha'] == NEW
    assert not any(candidate['components']['honua-server'].get(field)
                   for field in ('awsLambdaImage', 'awsLambdaDigest', 'awsLambdaEcrDigest'))
