#!/usr/bin/env python3
"""Govern saved Terraform plans. Never print Terraform logs, state or values."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = os.environ.get('PILOT_TERRAFORM_ROOT', 'certification/hosted-pilot')
if TF_ROOT not in ('certification/hosted-pilot', 'certification/hosted-pilot/bootstrap'):
    raise ValueError('unreviewed Terraform root')
TF = ROOT / TF_ROOT
BOOTSTRAP = TF_ROOT.endswith('/bootstrap')
SHA = re.compile(r'[a-f0-9]{64}')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def private_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')
    Path(path).chmod(0o600)


def run(args, *, log=None):
    result = subprocess.run(args, cwd=TF, capture_output=True, check=False)
    if log:
        Path(log).write_bytes(result.stdout + result.stderr)
        Path(log).chmod(0o600)
    require(result.returncode == 0, f'{args[0]} {args[1]} failed; private diagnostic retained only on executor')
    return result.stdout


def aws(*args):
    return json.loads(run(['aws', *args, '--output', 'json']))


def context():
    require(os.environ['GITHUB_REF'] == 'refs/heads/trunk', 'deployment must run from trunk')
    require(re.fullmatch(r'[a-f0-9]{40}', os.environ['GITHUB_SHA']), 'immutable workflow SHA required')
    require(os.environ['GITHUB_REPOSITORY'] == 'honua-io/honua-release', 'unexpected deployment repository')
    return {key: os.environ[key] for key in ('GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_REPOSITORY')}


def config(path):
    candidate = (ROOT / path).resolve()
    require(candidate.is_relative_to(TF / 'deployments') and candidate.name.endswith('.tfvars.json'), 'use a checked-in deployments/*.tfvars.json file')
    run(['git', 'ls-files', '--error-unmatch', str(candidate)])
    value = json.loads(candidate.read_text())
    require(value.get('region', 'us-west-2') == os.environ['PILOT_REGION'], 'configuration and workflow regions differ')
    require(isinstance(value, dict), 'configuration must be a JSON object')
    require(not any(re.search(r'password|token|api_key|client_secret|signing_key', key, re.I) for key in value if not key.endswith(('_arn', '_arns'))), 'only secret references are accepted')
    require(re.fullmatch(r'honua-pilot-[a-z0-9-]{4,16}', value.get('name_prefix', '')), 'isolated pilot prefix required')
    expiry = datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00'))
    require(expiry.tzinfo is not None, 'expiry must include a timezone')
    if os.environ['PILOT_OPERATION'] == 'apply':
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        require(0 < remaining <= 7 * 86400, 'apply expiry must be within seven days')
    return candidate


def backend():
    e = os.environ
    for key in ('PILOT_REGION', 'PILOT_STATE_BUCKET', 'PILOT_STATE_KMS_ARN', 'PILOT_STATE_KEY', 'PILOT_BACKEND_ROLE_ARN', 'PILOT_PLAN_BUCKET', 'PILOT_PLAN_KMS_ARN', 'PILOT_ACCOUNT_ID'):
        require(e.get(key) and 'REPLACE' not in e[key], f'missing reviewed binding: {key}')
    require(re.fullmatch(r'\d{12}', e['PILOT_ACCOUNT_ID']), 'explicit AWS account required')
    require(re.fullmatch(r'arn:aws:iam::' + e['PILOT_ACCOUNT_ID'] + r':role/[\w+=,.@/-]+', e['PILOT_BACKEND_ROLE_ARN']), 'backend role must belong to pilot account')
    require(e['PILOT_STATE_KEY'].startswith('honua/hosted-pilot/') and '..' not in e['PILOT_STATE_KEY'], 'isolated backend key required')
    require(re.fullmatch(r'arn:aws:kms:' + re.escape(e['PILOT_REGION']) + ':' + e['PILOT_ACCOUNT_ID'] + r':key/[a-f0-9-]+', e['PILOT_PLAN_KMS_ARN']), 'explicit regional plan KMS key required')
    values = {'bucket': e['PILOT_STATE_BUCKET'], 'key': e['PILOT_STATE_KEY'], 'region': e['PILOT_REGION'], 'encrypt': True, 'kms_key_id': e['PILOT_STATE_KMS_ARN'], 'use_lockfile': True, 'assume_role': {'role_arn': e['PILOT_BACKEND_ROLE_ARN']}}
    private_json(TF / 'backend.tf.json', {'terraform': {'backend': {'s3': values}}})
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def initialize(work):
    require(not any(key.startswith(('TF_VAR_', 'TF_CLI_ARGS')) for key in os.environ), 'ambient Terraform inputs/arguments refused')
    require(not any(TF.glob('*.auto.tfvars*')) and not (TF / 'terraform.tfvars').exists(), 'ambient variable files refused')
    require(not (TF / 'backend.tf').exists(), 'unreviewed backend configuration refused')
    backend_hash = backend()
    require(aws('sts', 'get-caller-identity')['Account'] == os.environ['PILOT_ACCOUNT_ID'], 'AWS account mismatch')
    run(['terraform', 'init', '-input=false', '-lockfile=readonly', '-no-color'], log=work / 'init.log')
    return backend_hash


def budget():
    # The approved pilot policy remains independent of the legacy RC campaign.
    policy = json.loads((ROOT / 'certification/agent-e2e/foss4g-pilot.v1.json').read_text())['policy']
    require((policy['totalBudgetUsd'], policy['cleanupReserveUsd'], policy['maxConcurrentPaidAttempts']) == (2000, 200, 4), 'pilot budget policy changed without separate owner decision')
    # A reviewed, immutable ledger object is supplied by the shared budget owner;
    # the deployment role is read-only on this prefix. No self-attested dispatch number.
    e = os.environ
    require(e.get('PILOT_BUDGET_VERSION_ID') and e.get('PILOT_BUDGET_SHA256'), 'versioned owner budget ledger required')
    require(SHA.fullmatch(e['PILOT_BUDGET_SHA256']), 'budget ledger digest required')
    target = Path(e['RUNNER_TEMP']) / 'pilot-budget.json'
    aws('s3api', 'get-object', '--bucket', e['PILOT_PLAN_BUCKET'], '--key', 'hosted-pilot/budget/ledger.json', '--version-id', e['PILOT_BUDGET_VERSION_ID'], str(target))
    require(digest(target) == e['PILOT_BUDGET_SHA256'], 'budget ledger digest mismatch')
    ledger = json.loads(target.read_text())
    require(ledger['schema'] == 'honua.hosted-pilot-budget/v1', 'unsupported budget ledger')
    require(datetime.fromisoformat(ledger['expires_at'].replace('Z', '+00:00')) > datetime.now(timezone.utc), 'budget ledger expired')
    require(ledger['total_budget_usd'] == 2000 and ledger['cleanup_reserve_usd'] == 200, 'ledger changes approved pilot limits')
    remaining = 1800 - ledger['spent_usd'] - ledger['reserved_usd']
    require(ledger['spent_usd'] >= 0 and ledger['reserved_usd'] >= 0 and ledger['deployment_reservation_usd'] > 0 and ledger['deployment_reservation_usd'] <= remaining, 'deployment would consume cleanup reserve or exceed pilot budget')
    require(0 <= ledger['active_paid_attempts'] < 4, 'pilot concurrency limit reached')
    require(ledger['source_sha'] == e['GITHUB_SHA'] and ledger['config_sha256'] == digest(config(e['PILOT_CONFIG_PATH'])), 'budget reservation must bind this reviewed configuration/source')
    # A ledger reservation can fund one workflow run only. The bucket policy
    # requires conditional writes and denies deletion/overwrite on this prefix.
    claim = {'run_id': e['GITHUB_RUN_ID'], 'source_sha': e['GITHUB_SHA'], 'ledger_sha256': e['PILOT_BUDGET_SHA256']}
    claim_path = Path(e['RUNNER_TEMP']) / 'pilot-budget-claim.json'
    private_json(claim_path, claim)
    claim_key = 'hosted-pilot/reservations/' + e['PILOT_BUDGET_SHA256'] + '.json'
    attempt = subprocess.run(['aws', 's3api', 'put-object', '--bucket', e['PILOT_PLAN_BUCKET'], '--key', claim_key, '--body', str(claim_path), '--if-none-match', '*', '--server-side-encryption', 'aws:kms', '--ssekms-key-id', e['PILOT_PLAN_KMS_ARN']], capture_output=True)
    if attempt.returncode != 0:
        aws('s3api', 'get-object', '--bucket', e['PILOT_PLAN_BUCKET'], '--key', claim_key, str(claim_path))
        require(json.loads(claim_path.read_text()) == claim, 'budget reservation already claimed by another run')
    return {'version_id': e['PILOT_BUDGET_VERSION_ID'], 'sha256': e['PILOT_BUDGET_SHA256']}


def custody():
    bucket = os.environ['PILOT_PLAN_BUCKET']
    require(aws('s3api', 'get-bucket-versioning', '--bucket', bucket).get('Status') == 'Enabled', 'plan bucket versioning required')
    block = aws('s3api', 'get-public-access-block', '--bucket', bucket)['PublicAccessBlockConfiguration']
    require(all(block.get(key) is True for key in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')), 'private plan bucket required')


def summarize(plan):
    # Explicit allowlist: never serialize before/after values or Terraform outputs.
    return [{'address': row['address'], 'actions': row['change']['actions']} for row in plan.get('resource_changes', [])]


def seal(work, cfg, backend_hash, budget_binding):
    e = os.environ
    review = {'schema': 'honua.hosted-pilot-saved-plan/v1', 'context': context(), 'operation': e['PILOT_OPERATION'], 'config_path': str(cfg.relative_to(ROOT)), 'config_sha256': digest(cfg), 'backend_sha256': backend_hash, 'root_path': TF_ROOT, 'lock_sha256': digest(TF / '.terraform.lock.hcl'), 'plan_sha256': digest(work / 'saved.tfplan'), 'budget': budget_binding, 'created_at': datetime.now(timezone.utc).isoformat()}
    plan = json.loads(run(['terraform', 'show', '-json', str(work / 'saved.tfplan')]))
    review['resource_changes'] = summarize(plan)
    review['action_counts'] = dict(Counter(action for row in review['resource_changes'] for action in row['actions']))
    return review


def verify(review, work, cfg, backend_hash):
    e = os.environ
    require(review['schema'] == 'honua.hosted-pilot-saved-plan/v1', 'unsupported saved plan')
    expected = context()
    # A re-run of only the approved apply job must keep the producing attempt.
    expected['GITHUB_RUN_ATTEMPT'] = e['PILOT_PLAN_ATTEMPT']
    require(review['context'] == expected, 'saved plan producer identity mismatch')
    for field, value in {'operation': e['PILOT_OPERATION'], 'config_path': str(cfg.relative_to(ROOT)), 'config_sha256': digest(cfg), 'backend_sha256': backend_hash, 'root_path': TF_ROOT, 'lock_sha256': digest(TF / '.terraform.lock.hcl'), 'plan_sha256': digest(work / 'saved.tfplan')}.items():
        require(review[field] == value, f'saved plan {field} mismatch')
    require(review['budget'] == {'version_id': e['PILOT_BUDGET_VERSION_ID'], 'sha256': e['PILOT_BUDGET_SHA256']}, 'budget binding changed')
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(review['created_at'])).total_seconds()
    require(0 <= age <= 24 * 3600, 'saved plan approval expired; create a fresh plan')
    require(digest(work / 'review.json') == e['PILOT_REVIEW_SHA256'], 'review artifact digest mismatch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['plan', 'apply'])
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    context()
    require(os.environ['PILOT_OPERATION'] in ('apply', 'destroy'), 'unsupported operation')
    work = Path(os.environ['RUNNER_TEMP']) / 'hosted-pilot-private'
    work.mkdir(mode=0o700, exist_ok=True)
    cfg = config(args.config)
    backend_hash = initialize(work)
    custody()
    binding = budget() if os.environ['PILOT_OPERATION'] == 'apply' and not BOOTSTRAP else {'version_id': os.environ['PILOT_BUDGET_VERSION_ID'], 'sha256': os.environ['PILOT_BUDGET_SHA256']}
    e = os.environ
    key = f"hosted-pilot/plans/{e['GITHUB_RUN_ID']}/{e['PILOT_PLAN_ATTEMPT']}/saved.tfplan"
    if args.action == 'plan':
        command = ['terraform', 'plan', '-input=false', '-no-color', f'-var-file={cfg}', f'-out={work / "saved.tfplan"}']
        if e['PILOT_OPERATION'] == 'destroy':
            command.append('-destroy')
        run(command, log=work / 'plan.log')
        review = seal(work, cfg, backend_hash, binding)
        uploaded = aws('s3api', 'put-object', '--bucket', e['PILOT_PLAN_BUCKET'], '--key', key, '--body', str(work / 'saved.tfplan'), '--server-side-encryption', 'aws:kms', '--ssekms-key-id', e['PILOT_PLAN_KMS_ARN'], '--if-none-match', '*')
        require(uploaded.get('VersionId') not in (None, 'null'), 'immutable plan object version required')
        review['s3_version_id'] = uploaded['VersionId']
        private_json(work / 'review.json', review)
        with open(e['GITHUB_OUTPUT'], 'a') as out:
            out.write(f"review_sha256={digest(work / 'review.json')}\nplan_attempt={e['PILOT_PLAN_ATTEMPT']}\n")
        with open(e['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write('Saved hosted-pilot plan awaits protected-environment review.\n\n')
            out.write(f"Operation: `{review['operation']}`; source: `{e['GITHUB_SHA']}`; plan SHA256: `{review['plan_sha256']}`\n\n")
            out.write('Review `review.json` for exact config, backend binding, immutable S3 version and resource actions. Terraform values and binary plan remain private.\n')
    else:
        review = json.loads((work / 'review.json').read_text())
        received = aws('s3api', 'get-object', '--bucket', e['PILOT_PLAN_BUCKET'], '--key', key, '--version-id', review['s3_version_id'], str(work / 'saved.tfplan'))
        require(received.get('ServerSideEncryption') == 'aws:kms' and received.get('SSEKMSKeyId') == e['PILOT_PLAN_KMS_ARN'], 'plan encryption identity mismatch')
        verify(review, work, cfg, backend_hash)
        run(['terraform', 'apply', '-input=false', '-no-color', str(work / 'saved.tfplan')], log=work / 'apply.log')
        # Terraform outputs remain on the executor; even later added secret outputs
        # must never enter a public workflow log or GitHub artifact.
        if e['PILOT_OPERATION'] == 'apply':
            outputs = json.loads(run(['terraform', 'output', '-json']))
            private_json(work / 'outputs.json', outputs)
        print('Exact reviewed saved plan applied. Authentication qualification remains a separate gate.')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f'hosted pilot refused: {error}', file=sys.stderr)
        sys.exit(1)
