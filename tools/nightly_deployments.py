#!/usr/bin/env python3
"""Produce fresh deployment proof from unadmitted inputs; never mint a candidate."""
from __future__ import annotations

import argparse
import copy
import re
import hashlib
import json
from pathlib import Path
import subprocess

import yaml

import deployment_qualification as qualification
from nightly_receipts import Gh, dispatch, verify_run, wait_for_run

SERVER_REPOSITORY = 'honua-io/honua-server'
LAMBDA_WORKFLOW = 'lambda-preview-certification.yml'
LAMBDA_ARTIFACT = 'lambda-preview-certification-receipt'


def lambda_inputs(manifest: dict, envelope: bytes) -> dict[str, str]:
    server = manifest['components']['honua-server']
    prepared = {
        'schema': 'honua.prepared-deployment-manifest/v1',
        'manifest': qualification.execution_manifest(manifest),
        'operatingEnvelope': {
            'path': 'docs/2026.1-operating-envelope.md',
            'sha256': 'sha256:' + hashlib.sha256(envelope).hexdigest(),
        },
    }
    # Data only: the receiving trunk workflow supplies all executable producer code.
    result = {
        'source_image': server['awsLambdaImage'],
        'source_digest': server['awsLambdaDigest'],
        'architecture': server['awsLambdaArchitecture'],
        'server_revision': server['sha'],
        'prepared_manifest': json.dumps(prepared, separators=(',', ':'), ensure_ascii=False, allow_nan=False),
    }
    if len(json.dumps(result).encode()) > 60_000:
        raise ValueError('prepared Lambda inputs exceed the bounded workflow dispatch payload')
    return result


def dispatch_lambda(api, manifest: dict, envelope: bytes) -> int:
    run_id = dispatch(api, SERVER_REPOSITORY, LAMBDA_WORKFLOW, 'refs/heads/trunk',
                      lambda_inputs(manifest, envelope))
    verify_run(api('GET', f'repos/{SERVER_REPOSITORY}/actions/runs/{run_id}'),
               run_id=run_id, workflow=LAMBDA_WORKFLOW, head_branch='trunk')
    return run_id


def lambda_artifact(api, run_id: int) -> dict:
    result = api('GET', f'repos/{SERVER_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100')
    artifacts = result.get('artifacts', [])
    if result.get('total_count', 0) > 100:
        raise ValueError('unexpected Lambda artifact inventory size')
    matches = [item for item in artifacts if item.get('name') == LAMBDA_ARTIFACT and not item.get('expired')]
    if len(matches) != 1:
        raise ValueError('expected exactly one unexpired Lambda producer artifact')
    artifact = matches[0]
    if type(artifact.get('id')) is not int or artifact['id'] <= 0:
        raise ValueError('Lambda artifact has no immutable id')
    return artifact



def final_lambda_manifest(requested: dict, captured: dict, raw: dict) -> dict:
    """Only a measured ECR bootstrap output may change the prepared manifest."""
    final = copy.deepcopy(requested)
    server = final['components']['honua-server']
    actual = raw.get('artifact', {}).get('ecrDigest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', actual):
        raise ValueError('Lambda producer did not observe an exact ECR digest')
    if server.get('awsLambdaEcrDigest') == 'pending-ecr-mirror':
        server['awsLambdaEcrDigest'] = actual
    elif server.get('awsLambdaEcrDigest') != actual:
        raise ValueError('Lambda producer changed an already bound ECR digest')
    if qualification.execution_inputs(final, captured['deployment']) != captured:
        raise ValueError('Lambda producer captured different prepared inputs')
    return final


def produce_lambda(api, manifest_path: Path, envelope_path: Path, out: Path) -> None:
    manifest = yaml.safe_load(manifest_path.read_text())
    envelope = envelope_path.read_bytes()
    run_id = dispatch_lambda(api, manifest, envelope)
    print(f'Lambda producer: https://github.com/{SERVER_REPOSITORY}/actions/runs/{run_id}', flush=True)
    run = wait_for_run(api, SERVER_REPOSITORY, run_id, timeout=3300)
    verify_run(run, run_id=run_id, workflow=LAMBDA_WORKFLOW, head_branch='trunk')
    if run.get('conclusion') != 'success':
        raise ValueError('Lambda producer did not succeed; no qualification record emitted')
    artifact = lambda_artifact(api, run_id)
    out.mkdir(parents=True, exist_ok=False)
    subprocess.run(['gh', 'run', 'download', str(run_id), '--repo', SERVER_REPOSITORY,
                    '--name', LAMBDA_ARTIFACT, '--dir', str(out / 'producer')], check=True)
    inputs = qualification.read_json(out / 'producer/deployment-execution-inputs.json')
    raw = qualification.read_json(out / 'producer/lambda-preview-receipt.json')
    final = final_lambda_manifest(manifest, inputs, raw)
    # The canonical verifier independently authenticates the actual artifact bytes and run.
    subprocess.run(['python3', str(Path(__file__).with_name('deployment_qualification.py')), 'record',
                    '--inputs', str(out / 'producer/deployment-execution-inputs.json'),
                    '--raw-receipt', str(out / 'producer/lambda-preview-receipt.json'),
                    '--artifact-id', str(artifact['id']), '--out', str(out / 'receipt.json')], check=True)
    receipt = qualification.read_json(out / 'receipt.json')
    (out / 'platform-manifest.yaml').write_text(yaml.safe_dump(final, sort_keys=False))
    record = {'inputs': inputs, 'receipt': receipt}
    (out / 'lambda-record.json').write_text(json.dumps(record, indent=2, allow_nan=False) + '\n')



def produce_eks(api, prepared_ref: str, out: Path) -> None:
    if not re.fullmatch(r'[0-9a-f]{40}', prepared_ref):
        raise ValueError('prepared ref must be an immutable snapshot SHA')
    repository, workflow = 'honua-io/honua-release', 'e2e-cloud-aws.yml'
    run_id = dispatch(api, repository, workflow, 'refs/heads/trunk', {
        'prepared_ref': prepared_ref, 'target': 'aws-eks', 'redis_mode': 'both',
        'require_real': 'true', 'run_iac_live': 'false', 'cost_ceiling_usd': '20',
    })
    print(f'EKS producer: https://github.com/{repository}/actions/runs/{run_id}', flush=True)
    verify_run(api('GET', f'repos/{repository}/actions/runs/{run_id}'),
               run_id=run_id, workflow=workflow, head_branch='trunk')
    run = wait_for_run(api, repository, run_id, timeout=18000)
    verify_run(run, run_id=run_id, workflow=workflow, head_branch='trunk')
    if run.get('conclusion') != 'success':
        raise ValueError('EKS producer did not succeed; no qualification emitted')
    inventory = api('GET', f'repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100')
    if inventory.get('total_count', 0) > 100:
        raise ValueError('unexpected EKS artifact inventory size')
    out.mkdir(parents=True, exist_ok=False)
    records = {}
    for mode in ('off', 'on'):
        name = f'e2e-cloud-aws-gate-report-aws-eks-redis-{mode}'
        matches = [item for item in inventory.get('artifacts', [])
                   if item.get('name') == name and not item.get('expired')]
        if len(matches) != 1:
            raise ValueError(f'expected exactly one actual {mode} cell artifact')
        artifact = matches[0]
        directory = out / mode
        subprocess.run(['gh', 'run', 'download', str(run_id), '--repo', repository,
                        '--name', name, '--dir', str(directory)], check=True)
        captures = list(directory.rglob('deployment-execution-inputs.json'))
        if len(captures) != 1:
            raise ValueError(f'expected one before-deployment capture for Redis {mode}')
        capture = captures[0]
        raw = capture.with_name('deployment-cell-receipt.json')
        receipt_path = out / f'{mode}-receipt.json'
        subprocess.run(['python3', str(Path(__file__).with_name('deployment_qualification.py')), 'record',
                        '--inputs', str(capture), '--raw-receipt', str(raw),
                        '--artifact-id', str(artifact['id']), '--inputs-path', capture.relative_to(directory).as_posix(),
                        '--raw-receipt-path', raw.relative_to(directory).as_posix(), '--out', str(receipt_path)], check=True)
        inputs = qualification.read_json(capture)
        if inputs['deployment']['target'] != 'awsEks' or inputs['deployment']['configuration']['redisMode'] != mode:
            raise ValueError('EKS artifact names a different target or Redis mode')
        records[f'redis-{mode}'] = {'inputs': inputs, 'receipt': qualification.read_json(receipt_path)}
    (out / 'eks-record.json').write_text(json.dumps({'cells': records}, indent=2, allow_nan=False) + '\n')


def ga_scoped(matrix: dict, component: str, target: str) -> bool:
    rows = matrix['deploy'][component][target]['architectures']
    return any(row.get('releaseScope') == 'ga-target' for row in rows.values())


def assemble(manifest: Path, matrix: Path, envelope: Path, lambda_record: Path | None, eks_record: Path | None,
             out: Path) -> None:
    source_matrix = yaml.safe_load(matrix.read_text())
    result = {'schema': qualification.BUNDLE_SCHEMA, 'records': {}}
    # Founder decision 2026-10-25: Lambda is Preview, like EKS (2026-10-16). A Preview target's
    # proof is not required to admit a candidate; a GA-scoped target's proof always is.
    lambda_required = ga_scoped(source_matrix, 'honua-server', 'awsLambda')
    if lambda_required and lambda_record is None:
        raise ValueError('reviewed Lambda GA scope requires the actual Lambda pre-cut proof')
    if lambda_record is not None:
        result['records']['awsLambda'] = qualification.read_json(lambda_record)
    eks_required = ga_scoped(source_matrix, 'honua-helm', 'awsEks')
    if eks_required and eks_record is None:
        raise ValueError('reviewed EKS GA scope requires both complete qualification cells')
    if eks_record is not None:
        result['records']['awsEks'] = qualification.read_json(eks_record)
    # Every supplied record is verified; Preview neither supplies nor fabricates GA evidence.
    verified = qualification.verify_bundle(result, yaml.safe_load(manifest.read_text()), envelope.read_bytes())
    updated_matrix = (qualification.project_verified_matrix(source_matrix, verified)
                      if eks_required else source_matrix)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')
    (out.parent / 'compatibility-matrix.yaml').write_text(yaml.safe_dump(updated_matrix, sort_keys=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    lane = commands.add_parser('lambda')
    lane.add_argument('--manifest', type=Path, required=True)
    lane.add_argument('--operating-envelope', type=Path, required=True)
    lane.add_argument('--out-dir', type=Path, required=True)
    eks = commands.add_parser('eks')
    eks.add_argument('--prepared-ref', required=True)
    eks.add_argument('--out-dir', type=Path, required=True)
    join = commands.add_parser('assemble')
    join.add_argument('--manifest', type=Path, required=True)
    join.add_argument('--matrix', type=Path, required=True)
    join.add_argument('--operating-envelope', type=Path, required=True)
    join.add_argument('--lambda-record', type=Path)
    join.add_argument('--eks-record', type=Path)
    join.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'lambda':
        produce_lambda(Gh(), args.manifest, args.operating_envelope, args.out_dir)
    elif args.command == 'eks':
        produce_eks(Gh(), args.prepared_ref, args.out_dir)
    else:
        assemble(args.manifest, args.matrix, args.operating_envelope, args.lambda_record, args.eks_record, args.out)


if __name__ == '__main__':
    main()
