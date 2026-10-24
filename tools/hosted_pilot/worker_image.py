#!/usr/bin/env python3
"""Build a selected public A/B worker image; never export controller or oracle files."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_knowledge import export, REPOSITORY
from agent_e2e_pack import canonical_digest, digest_bytes, load_json
from bundle_agent_e2e_container import bundle, verify_runtime_index
from controller_image import call
from deploy import ROOT, budget, private_json, require
from image_retention import verify as verify_retention


def configuration(path):
    require(path.resolve() == ROOT/'certification/agent-e2e/worker-image.json' and path.is_file() and not path.is_symlink(), 'fixed reviewed worker image source required')
    call(['git', 'ls-files', '--error-unmatch', '--', str(path)], capture=True)
    value = load_json(path)
    require(set(value) == {'schemaVersion', 'caseId', 'pack', 'knowledge'} and value['schemaVersion'] == 1 and
            value['pack']['path'] in ('certification/agent-e2e/pack.v1.json', 'certification/agent-e2e/pack.v2.json') and
            re.fullmatch(r'sha256:[a-f0-9]{64}', value['pack']['sha256']) and
            digest_bytes((ROOT/value['pack']['path']).read_bytes()) == value['pack']['sha256'] and
            value['knowledge']['sourceRepository'] == REPOSITORY and
            re.fullmatch(r'[a-f0-9]{40}', value['knowledge']['sourceRevision']), 'reviewed public pack/knowledge source differs')
    return value


def claim_publication(original_budget):
    """Reserve exactly one physical build, including across workflow reruns.

    The generic deployment claim admits retries of one run. Image building is
    a separate paid effect, so even an identical retry or an ambiguous write
    cannot start a second build against the original reservation.
    """
    e = os.environ
    raw = (Path(e['RUNNER_TEMP'])/'pilot-budget.json').read_bytes()
    require(digest_bytes(raw) == 'sha256:'+original_budget['sha256'], 'original image reservation bytes changed')
    ledger = json.loads(raw)
    reservation = ledger.get('reservation_id') or original_budget['sha256']
    require(isinstance(reservation, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{2,119}', reservation),
            'original image reservation identity is invalid')
    require(re.fullmatch(r'[a-f0-9]{40}', e['GITHUB_SHA']) and
            all(re.fullmatch(r'[1-9][0-9]*', e[name]) for name in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')),
            'worker publication claim needs its exact source/run/attempt')
    claim = {'schema': 'honua.hosted-worker-publication-claim/v1', 'sourceSha': e['GITHUB_SHA'],
        'runId': e['GITHUB_RUN_ID'], 'runAttempt': e['GITHUB_RUN_ATTEMPT'], 'ownerBudget': original_budget,
        'repository': e['PILOT_WORKER_REPOSITORY']}
    path = Path(e['RUNNER_TEMP'])/'worker-publication-claim.json'
    private_json(path, claim)
    key = 'hosted-pilot/reservations/worker-image/'+reservation+'.json'
    result = subprocess.run(['aws', 's3api', 'put-object', '--bucket', e['PILOT_PLAN_BUCKET'], '--key', key,
        '--body', str(path), '--if-none-match', '*', '--server-side-encryption', 'aws:kms',
        '--ssekms-key-id', e['PILOT_PLAN_KMS_ARN'], '--output', 'json'], capture_output=True)
    require(result.returncode == 0, 'worker publication is already claimed or its claim is ambiguous; no build admitted')
    try:
        observed = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError('worker publication claim result is ambiguous; no build admitted') from error
    require(isinstance(observed, dict) and isinstance(observed.get('VersionId'), str) and
        observed['VersionId'] not in ('', 'null') and observed.get('ServerSideEncryption') == 'aws:kms' and
        observed.get('SSEKMSKeyId') == e['PILOT_PLAN_KMS_ARN'],
        'worker publication claim has no immutable encrypted version; no build admitted')
    return {'bucket': e['PILOT_PLAN_BUCKET'], 'key': key, 'versionId': observed['VersionId'],
        'sha256': digest_bytes(path.read_bytes())}


def main():
    e = os.environ
    require(e['GITHUB_REPOSITORY'] == 'honua-io/honua-release' and e['GITHUB_REF'] == 'refs/heads/trunk' and
            re.fullmatch(r'[a-f0-9]{40}', e['GITHUB_SHA']) and call(['git', 'rev-parse', 'HEAD'], capture=True) == e['GITHUB_SHA'],
            'worker images require the exact protected release source')
    config = configuration(ROOT/'certification/agent-e2e/worker-image.json')
    repository = e['PILOT_WORKER_REPOSITORY']
    prefix = e['PILOT_ACCOUNT_ID']+'.dkr.ecr.'+e['PILOT_REGION']+'.amazonaws.com/'
    require(e['PILOT_ACCOUNT_ID'] == '585192672263' and e['PILOT_REGION'] == 'us-east-1' and repository.startswith(prefix) and
            re.fullmatch(r'honua-pilot-[a-z0-9-]+-worker', repository[len(prefix):]), 'worker repository must be the existing isolated foundation destination')
    require(json.loads(call(['aws', 'sts', 'get-caller-identity', '--output', 'json'], capture=True))['Account'] == e['PILOT_ACCOUNT_ID'], 'worker publisher account differs')
    retention = verify_retention(repository[len(prefix):], e['PILOT_ACCOUNT_ID'], e['PILOT_REGION'], call=call)
    # The existing create-only owner reservation can fund only this workflow run.
    # Its immutable source SHA also binds worker-image.json and selected exports.
    original_budget = budget()
    publication_claim = claim_publication(original_budget)
    tag = e['GITHUB_SHA']+'-'+e['GITHUB_RUN_ID']+'-'+e['GITHUB_RUN_ATTEMPT']
    require(re.fullmatch(r'[a-f0-9]{40}-[1-9][0-9]*-[1-9][0-9]*', tag), 'worker publication requires actual producer run/attempt')
    image = repository+':'+tag
    with tempfile.TemporaryDirectory(prefix='pilot-worker-', dir=e['RUNNER_TEMP']) as folder:
        folder = Path(folder)
        docs, knowledge, context = ROOT/'worker-knowledge-source', folder/'knowledge', folder/'context'
        require(call(['git', '-C', str(docs), 'rev-parse', 'HEAD'], capture=True) == config['knowledge']['sourceRevision'],
                'authenticated documentation checkout differs from reviewed knowledge source')
        actual = export(docs, config['knowledge']['sourceRevision'], load_json(ROOT/config['pack']['path']), knowledge)
        require(actual == config['knowledge'], 'public OKF export differs from exact reviewed source binding')
        index = bundle(context, knowledge_root=knowledge, pack_path=ROOT/config['pack']['path'], case_id=config['caseId'])
        public_digest = digest_bytes((context/'public/public-manifest.json').read_bytes())
        verify_runtime_index(index)
        require(index['files'].get('public/public-manifest.json') == public_digest,
                'published runtime index must retain the exact public manifest')
        call(['docker', 'build', '--platform', 'linux/amd64', '-t', image, str(context)])
        size = int(call(['docker', 'image', 'inspect', '--format', '{{.Size}}', image], capture=True))
        require(0 < size <= 512*1024*1024, 'worker image exceeds its bounded artifact-storage liability')
        smoke = ROOT/'tools/smoke_agent_e2e_container.py'
        call(['docker', 'run', '--rm', '--read-only', '--network', 'none', '--tmpfs', '/workspace:rw,uid=10001,gid=10001',
              '--tmpfs', '/journal:rw,uid=10001,gid=10001', '-v', str(smoke)+':/smoke.py:ro', '--entrypoint', 'python', image, '/smoke.py'])
        password = subprocess.run(['aws', 'ecr', 'get-login-password', '--region', e['PILOT_REGION']], capture_output=True)
        require(password.returncode == 0, 'worker registry login refused')
        require(verify_retention(repository[len(prefix):], e['PILOT_ACCOUNT_ID'], e['PILOT_REGION'], call=call) == retention,
                'worker artifact expiry changed before publication')
        call(['docker', 'login', '--username', 'AWS', '--password-stdin', repository.split('/')[0]], input=password.stdout)
        try:
            call(['docker', 'push', image])
        finally:
            call(['docker', 'logout', repository.split('/')[0]])
    image_digest = call(['aws', 'ecr', 'describe-images', '--region', e['PILOT_REGION'], '--repository-name', repository[len(prefix):],
        '--image-ids', 'imageTag='+tag, '--query', 'imageDetails[0].imageDigest', '--output', 'text'], capture=True)
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', image_digest), 'actual immutable worker image digest missing')
    receipt = {'schema': 'honua.hosted-worker-image/v1', 'sourceSha': e['GITHUB_SHA'], 'runId': e['GITHUB_RUN_ID'], 'runAttempt': e['GITHUB_RUN_ATTEMPT'],
        'image': repository+'@'+image_digest, 'imageBytes': size, 'platform': 'linux/amd64', 'caseId': config['caseId'], 'pack': config['pack'],
        'knowledge': actual, 'publicBundleDigest': public_digest, 'runtimeBundleDigest': canonical_digest(index), 'ownerBudget': original_budget,
        'retention': retention, 'publicationClaim': publication_claim, 'qualification': False, 'modelExecution': 'not-run'}
    # Retain the original index alongside the image receipt in one Actions
    # artifact. Installation authenticates that ZIP before retaining its bytes;
    # image publication does not require a candidate accounting bucket.
    (Path(e['RUNNER_TEMP'])/'runtime-index.json').write_text(json.dumps(index, sort_keys=True)+'\n', encoding='utf-8')
    (Path(e['RUNNER_TEMP'])/'hosted-pilot-worker-image.json').write_text(json.dumps(receipt, sort_keys=True)+'\n', encoding='utf-8')
    print('Selected public A/B worker image published. Real native worker/model/cleanup evidence remains required.')


if __name__ == '__main__':
    main()
