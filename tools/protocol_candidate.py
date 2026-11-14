#!/usr/bin/env python3
"""Derive protocol evaluator inputs and its receipt from an authenticated candidate freeze."""
import argparse
import json
from pathlib import Path

import yaml

from agent_e2e_candidate import authenticate, require


def prepare(root, run_id, attempt, digest, *, verifier=authenticate):
    root = Path(root)
    verified = verifier(root, run_id, attempt)
    require(verified['qualificationLockDigest'] == digest, 'candidate lock differs from requested digest')
    manifest = yaml.safe_load((root/'platform-manifest.yaml').read_text())
    components, artifacts = manifest['components'], manifest['clientArtifacts']
    certification = manifest['protocolCertification']
    ledger = certification['ledger']
    server = components['honua-server']
    values = {'matrix_repository': ledger['repository'], 'matrix_commit': ledger['commit'],
        'matrix_path': ledger['path'], 'matrix_sha256': ledger['sha256'],
        'expected_requirements_source_revision': ledger['requirementsSourceRevision'],
        'expected_source_sha': server['sha'], 'expected_image_digest': server['digest'],
        'expected_cut_at': certification['candidateCutAt'],
        'expected_server_certification_sha': certification['serverCertificationProducerSha']}
    for short, component in [('sdk_js', 'honua-sdk-js'), ('sdk_python', 'honua-sdk-python'),
                             ('sdk_dotnet', 'honua-sdk-dotnet'), ('geospatial_grpc', 'geospatial-grpc'),
                             ('geospatial_mcp', 'geospatial-mcp')]:
        values['expected_'+short+'_sha'] = components[component]['sha']
    for short, artifact in [('sdk_js', 'honua-sdk-js'), ('sdk_python', 'honua-sdk-python-wheel'),
                            ('sdk_dotnet', 'honua-sdk-dotnet')]:
        values['expected_'+short+'_version'] = artifacts[artifact]['version']
    require(all(isinstance(v, str) and v and '\n' not in v and '\r' not in v for v in values.values()),
            'protocol inputs must be nonempty single-line frozen values')
    return {'schemaVersion': 1, 'serverSha': server['sha'], 'imageDigest': server['digest'],
            'platformLockDigest': digest, 'producer': verified['producer'], 'inputs': values}


def receipt(identity, conclusion, status):
    return {key: value for key, value in identity.items() if key != 'inputs'} | {
        'status': 'pass' if conclusion == 'success' and status == 'pass' else 'fail'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--run-id', type=int)
    parser.add_argument('--attempt', type=int)
    parser.add_argument('--digest')
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--github-output', type=Path)
    parser.add_argument('--conclusion')
    parser.add_argument('--status')
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    if args.receipt:
        result = receipt(json.loads(args.identity.read_text()), args.conclusion, args.status)
        args.receipt.write_text(json.dumps(result, sort_keys=True)+'\n')
    else:
        result = prepare(args.root, args.run_id, args.attempt, args.digest)
        args.identity.write_text(json.dumps(result, sort_keys=True)+'\n')
        with args.github_output.open('a') as output:
            for key, value in result['inputs'].items():
                output.write(f'{key}={value}\n')


if __name__ == '__main__':
    main()
