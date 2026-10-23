#!/usr/bin/env python3
"""Custody-first foundation bootstrap; never upload plans, state or logs to GitHub."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from deploy import TF, ROOT, require, digest, private_json, run, aws, summarize

CUSTODY = {
    'aws_kms_key.custody', 'aws_s3_bucket.custody',
    'aws_s3_bucket_public_access_block.custody', 'aws_s3_bucket_ownership_controls.custody',
    'aws_s3_bucket_versioning.custody', 'aws_s3_bucket_server_side_encryption_configuration.custody',
    'aws_s3_bucket_policy.custody', 'aws_s3_bucket_lifecycle_configuration.plans',
}


def allowed_address(address):
    base = address.split('[', 1)[0]
    return base in CUSTODY and (address == base or address in (base + '["state"]', base + '["plans"]'))


def exists(*args):
    result = subprocess.run(['aws', 's3api', *args], capture_output=True)
    if result.returncode:
        require(b'404' in result.stderr or b'Not Found' in result.stderr or b'NoSuchKey' in result.stderr, 'cannot distinguish absent custody from authorization failure')
        return False
    return True


def optional_aws(*args):
    result = subprocess.run(['aws', *args, '--output', 'json'], capture_output=True)
    if result.returncode:
        absent = (b'NoSuchBucketPolicy', b'NoSuchPublicAccessBlockConfiguration', b'OwnershipControlsNotFoundError', b'NoSuchLifecycleConfiguration', b'NoSuchTagSet')
        if any(code in result.stderr for code in absent):
            return None
        raise ValueError('custody discovery API failed; authorization errors are not absence')
    return json.loads(result.stdout)


def secure_backend(binding):
    bucket = binding['bucket']
    prefix = binding['key'].split('/')[2]
    tags = {tag['Key']: tag['Value'] for tag in aws('s3api', 'get-bucket-tagging', '--bucket', bucket).get('TagSet', [])}
    require(tags.get('ManagedBy') == 'honua-release-hosted-pilot-bootstrap' and tags.get('ResetBoundary') == prefix, 'existing custody bucket ownership tags differ; retain liability and refuse adoption')
    rules = aws('s3api', 'get-bucket-encryption', '--bucket', bucket)['ServerSideEncryptionConfiguration']['Rules']
    default = rules[0]['ApplyServerSideEncryptionByDefault']
    require(default['SSEAlgorithm'] == 'aws:kms', 'bootstrap backend must use KMS encryption')
    account = os.environ['PILOT_BOOTSTRAP_ACCOUNT_ID']
    require(re.fullmatch('arn:aws:kms:' + re.escape(binding['region']) + ':' + account + r':key/[a-f0-9-]+', default['KMSMasterKeyID']), 'custody KMS key is outside the reviewed account/region')
    key_tags = {tag['TagKey']: tag['TagValue'] for tag in aws('kms', 'list-resource-tags', '--key-id', default['KMSMasterKeyID']).get('Tags', [])}
    require(key_tags.get('ManagedBy') == 'honua-release-hosted-pilot-bootstrap' and key_tags.get('ResetBoundary') == prefix, 'custody KMS ownership tags differ')
    block = aws('s3api', 'get-public-access-block', '--bucket', bucket)['PublicAccessBlockConfiguration']
    require(all(block.get(key) is True for key in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')), 'bootstrap custody is not private')
    require(aws('s3api', 'get-bucket-versioning', '--bucket', bucket).get('Status') == 'Enabled', 'bootstrap custody must be versioned')
    return binding | {'kms_key_id': default['KMSMasterKeyID']}


def journal(cfg, identity, work, *, remote_state=None):
    # Custody contains AWS-owned KMS material and metadata only. Preserve only
    # import IDs, never Terraform state, values, attributes, plans or logs.
    rows = []
    state_file = TF / 'terraform.tfstate'
    if state_file.exists():
        state = json.loads(state_file.read_text())
        for resource in state.get('resources', []):
            if resource.get('mode') != 'managed' or resource.get('module'):
                continue
            base = resource['type'] + '.' + resource['name']
            if base not in CUSTODY:
                continue
            for instance in resource.get('instances', []):
                address = base
                if 'index_key' in instance:
                    address += '[' + json.dumps(instance['index_key']) + ']'
                require(allowed_address(address), 'noncustody resource in initial recovery journal')
                resource_id = instance.get('attributes', {}).get('id')
                if resource_id:
                    rows.append({'address': address, 'id': resource_id})
    # AWS can acknowledge creation after cancellation but before Terraform has
    # flushed local state. Discover only owned, deterministic custody names and
    # the creation-tagged key; never infer application/identity resources.
    discovered = set(row['address'] for row in rows)
    discovery_complete = True
    unmapped_owned_keys = []
    unverified_buckets = []
    try:
        if 'aws_kms_key.custody' not in discovered:
            matches = []
            for key in aws('kms', 'list-keys').get('Keys', []):
                tags = {tag['TagKey']: tag['TagValue'] for tag in aws('kms', 'list-resource-tags', '--key-id', key['KeyId']).get('Tags', [])}
                if tags.get('ManagedBy') == 'honua-release-hosted-pilot-bootstrap' and tags.get('ResetBoundary') == cfg['name_prefix'] and tags.get('BootstrapExecution') == os.environ['GITHUB_RUN_ID']:
                    matches.append(key['KeyId'])
            if len(matches) == 1:
                rows.append({'address': 'aws_kms_key.custody', 'id': matches[0]})
            elif len(matches) > 1:
                discovery_complete = False
                unmapped_owned_keys = matches
        owned = {bucket['Name'] for bucket in aws('s3api', 'list-buckets').get('Buckets', [])}
        account = os.environ.get('PILOT_BOOTSTRAP_ACCOUNT_ID', '')
        for kind in ['state', 'plans']:
            bucket = f'honua-release-{cfg["name_prefix"].removeprefix("honua-")}-foundation-{kind}-{account}'
            if bucket not in owned:
                continue
            observed = optional_aws('s3api', 'get-bucket-tagging', '--bucket', bucket)
            tags = {tag['Key']: tag['Value'] for tag in (observed or {}).get('TagSet', [])}
            if tags.get('ManagedBy') != 'honua-release-hosted-pilot-bootstrap' or tags.get('ResetBoundary') != cfg['name_prefix']:
                unverified_buckets.append(bucket)
                discovery_complete = False
                continue
            address = 'aws_s3_bucket.custody[' + json.dumps(kind) + ']'
            if address not in discovered:
                rows.append({'address': address, 'id': bucket})
            controls = {
                'aws_s3_bucket_public_access_block.custody': 'get-public-access-block',
                'aws_s3_bucket_ownership_controls.custody': 'get-bucket-ownership-controls',
                'aws_s3_bucket_server_side_encryption_configuration.custody': 'get-bucket-encryption',
                'aws_s3_bucket_policy.custody': 'get-bucket-policy',
            }
            for base, getter in controls.items():
                address = base + '[' + json.dumps(kind) + ']'
                if address in discovered:
                    continue
                if optional_aws('s3api', getter, '--bucket', bucket) is not None:
                    rows.append({'address': address, 'id': bucket})
            versioning = aws('s3api', 'get-bucket-versioning', '--bucket', bucket)
            address = 'aws_s3_bucket_versioning.custody[' + json.dumps(kind) + ']'
            if versioning.get('Status') and address not in discovered:
                rows.append({'address': address, 'id': bucket})
            if kind == 'plans' and 'aws_s3_bucket_lifecycle_configuration.plans' not in discovered:
                if optional_aws('s3api', 'get-bucket-lifecycle-configuration', '--bucket', bucket) is not None:
                    rows.append({'address': 'aws_s3_bucket_lifecycle_configuration.plans', 'id': bucket})
    except (ValueError, KeyError, OSError):
        discovery_complete = False
    value = {'schema': 'honua.hosted-pilot-custody-recovery/v1', **identity,
             'bootstrap_run_id': os.environ['GITHUB_RUN_ID'],
             'resources': rows, 'remote_recovery_state': remote_state, 'cloud_discovery_complete': discovery_complete, 'unmapped_owned_key_ids': unmapped_owned_keys, 'unverified_bucket_names': unverified_buckets,
             'cloud_discovery_tags': {'ManagedBy': 'honua-release-hosted-pilot-bootstrap', 'ResetBoundary': cfg['name_prefix'], 'BootstrapExecution': os.environ['GITHUB_RUN_ID']},
             'foundation_backend': {'bucket': f'honua-release-{cfg["name_prefix"].removeprefix("honua-")}-foundation-state-' + os.environ.get('PILOT_BOOTSTRAP_ACCOUNT_ID', ''), 'key': f'honua/hosted-pilot/{cfg["name_prefix"]}/foundation.tfstate'},
             'status': 'recovery-required-until-remote-state-verified'}
    private_json(work / 'custody-recovery.json', value)
    return value


def bootstrap_contract_digest():
    # Exclude the compatibility map itself to avoid a self-referential commit
    # hash. The selected root SHA is independently bound to reviewed trunk.
    base = ROOT / 'certification/hosted-pilot/bootstrap'
    paths = sorted(path for path in base.glob('*.tf') if path.name != 'recovery-imports.tf')
    paths += [base / '.terraform.lock.hcl', base / 'foundation-bootstrap-workflow.yml', ROOT / 'tools/hosted_pilot/bootstrap.py', ROOT / 'tools/hosted_pilot/deploy.py']
    value = hashlib.sha256()
    for path in paths:
        value.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes() + b'\0')
    return value.hexdigest()


def recovery_compatible(value, identity):
    if all(value.get(key) == val for key, val in identity.items()):
        return True
    policy = json.loads((ROOT / 'certification/hosted-pilot/bootstrap/recovery-compatibility.json').read_text())
    require(policy['schema'] == 'honua.hosted-pilot-recovery-compatibility/v1', 'unsupported recovery compatibility policy')
    for entry in policy['migrations']:
        if (entry['from_release_root_sha'] == value.get('release_root_sha') and
            entry['from_foundation_source_sha'] == value.get('foundation_source_sha') and
            entry['config_sha256'] == value.get('config_sha256') == identity['config_sha256'] and
            entry['from_provider_lock_sha256'] == value.get('provider_lock_sha256') and
            entry['to_provider_lock_sha256'] == identity['provider_lock_sha256'] and
            entry['to_bootstrap_contract_sha256'] == bootstrap_contract_digest() and
            value.get('config_path') == identity['config_path'] and
            isinstance(entry.get('reason'), str) and entry['reason'].strip()):
            return True
    return False


def recover_imports(identity, work):
    path = work / 'recovery-input/custody-recovery.json'
    if not path.exists():
        return
    require(digest(path) == os.environ['PILOT_RECOVERY_SHA256'], 'custody recovery journal digest mismatch')
    value = json.loads(path.read_text())
    require(value['schema'] == 'honua.hosted-pilot-custody-recovery/v1' and recovery_compatible(value, identity), 'custody recovery source/config identity mismatch; a reviewed compatibility mapping is required for a fixed helper')
    require(value['bootstrap_run_id'] == os.environ['PILOT_RECOVERY_RUN_ID'], 'custody recovery producer mismatch')
    producer = json.loads((Path(os.environ['RUNNER_TEMP']) / 'recovery-producer.json').read_text())
    require(producer['head_sha'] == value['foundation_source_sha'] and str(producer['id']) == value['bootstrap_run_id'], 'journal source differs from its observed producing run')
    require(value.get('cloud_discovery_complete') is True, 'initial custody discovery incomplete; do not discard retained cloud resources or state')
    require(value['resources'], 'no imported custody identities; inspect the recorded cloud discovery tags before recovery')
    imports = []
    for row in value['resources']:
        require(allowed_address(row['address']) and isinstance(row['id'], str), 'unapproved custody import')
        imports.append('import {\n  to = ' + row['address'] + '\n  id = ' + json.dumps(row['id']) + '\n}\n')
    (TF / 'recovery-imports.tf').write_text('\n'.join(imports))


def save_recovery(binding, cfg, identity, work):
    remote = None
    if (TF / 'terraform.tfstate').exists():
        try:
            secured = secure_backend(binding)
            key = f"honua/hosted-pilot/{cfg['name_prefix']}/foundation-recovery/{os.environ['GITHUB_RUN_ID']}/{time.time_ns()}.tfstate"
            result = aws('s3api', 'put-object', '--bucket', secured['bucket'], '--key', key,
                         '--body', str(TF / 'terraform.tfstate'), '--server-side-encryption', 'aws:kms',
                         '--ssekms-key-id', secured['kms_key_id'], '--if-none-match', '*')
            require(result.get('VersionId') not in (None, 'null'), 'recovery state version missing')
            remote = {'bucket': secured['bucket'], 'key': key, 'version_id': result['VersionId'], 'kms_key_id': secured['kms_key_id'], 'sha256': digest(TF / 'terraform.tfstate')}
        except (ValueError, KeyError, OSError):
            # Never upload raw state to GitHub, even when initial custody failed.
            # The metadata-only import journal and AWS creation tags remain the
            # recovery boundary. No application/identity bootstrap ran yet.
            pass
    journal(cfg, identity, work, remote_state=remote)
    return remote


def plan(cfg_path, work, *, custody_only=False):
    path = work / ('custody.tfplan' if custody_only else 'saved.tfplan')
    cmd = ['terraform', 'plan', '-input=false', '-no-color', f'-var-file={cfg_path}',
           '-var=bootstrap_execution_id=' + os.environ['GITHUB_RUN_ID'], f'-out={path}']
    if custody_only:
        cmd += ['-target=' + address for address in sorted(CUSTODY)]
    run(cmd, log=work / 'plan.log')
    expected_sha = digest(path)
    changes = summarize(json.loads(run(['terraform', 'show', '-json', str(path)])))
    if custody_only:
        require(all(allowed_address(row['address']) or (row['address'] == 'data.aws_caller_identity.current' and row['actions'] in (['read'], ['no-op'])) for row in changes), 'initial custody plan attempts noncustody infrastructure')
    return path, changes, expected_sha


def exact_apply(path, expected, work):
    # The source-reviewed bootstrap applies only the locally produced binary.
    # It never accepts or uploads a binary plan supplied through workflow input.
    require(digest(path) == expected, 'bootstrap saved plan changed')
    run(['terraform', 'apply', '-input=false', '-no-color', str(path)], log=work / 'apply.log')
    return expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['plan', 'deploy', 'journal'])
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    e = os.environ
    require(e['GITHUB_REPOSITORY'] == 'honua-io/honua-iac' and e['GITHUB_REF'] == 'refs/heads/trunk', 'foundation runs only in honua-iac trunk')
    require(re.fullmatch(r'[a-f0-9]{40}', e['PILOT_RELEASE_ROOT_SHA']), 'exact reviewed release root SHA required')
    require(run(['git', 'rev-parse', 'HEAD']).decode().strip() == e['PILOT_RELEASE_ROOT_SHA'], 'checked-out release root differs')
    source_root = Path(e['PILOT_FOUNDATION_SOURCE_ROOT']).resolve()
    cfg_path = (source_root / args.config).resolve()
    require(cfg_path.is_relative_to(source_root / 'certification/hosted-pilot/bootstrap/deployments') and cfg_path.name.endswith('.tfvars.json'), 'reviewed foundation config required')
    run(['git', '-C', str(source_root), 'ls-files', '--error-unmatch', str(cfg_path)])
    require(run(['git', '-C', str(source_root), 'rev-parse', 'HEAD']).decode().strip() == e['GITHUB_SHA'], 'foundation config source mismatch')
    require(not any(key.startswith(('TF_VAR_', 'TF_CLI_ARGS')) for key in e), 'ambient Terraform configuration refused')
    os.umask(0o077)
    work = Path(e['RUNNER_TEMP']) / 'hosted-pilot-bootstrap-private'
    work.mkdir(mode=0o700, exist_ok=True)
    cfg = json.loads(cfg_path.read_text())
    require(cfg['region'] == e['AWS_REGION'], 'bootstrap region mismatch')
    require(re.fullmatch(r'honua-pilot-[a-z0-9-]{4,16}', cfg['name_prefix']), 'invalid isolated prefix')
    account = aws('sts', 'get-caller-identity')['Account']
    require(cfg['account_id'] == account, 'foundation AWS account differs from reviewed configuration')
    os.environ['PILOT_BOOTSTRAP_ACCOUNT_ID'] = account
    binding = {'bucket': f'honua-release-{cfg["name_prefix"].removeprefix("honua-")}-foundation-state-{account}',
               'key': f'honua/hosted-pilot/{cfg["name_prefix"]}/foundation.tfstate', 'region': cfg['region'], 'encrypt': True, 'use_lockfile': True}
    identity = {'foundation_source_sha': e['GITHUB_SHA'], 'release_root_sha': e['PILOT_RELEASE_ROOT_SHA'],
                'config_path': args.config, 'config_sha256': digest(cfg_path),
                'provider_lock_sha256': digest(TF / '.terraform.lock.hcl')}
    if args.mode == 'journal':
        # Also runs with always() after failed/cancelled initial apply.
        save_recovery(binding, cfg, identity, work)
        with open(e['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('Custody recovery metadata SHA256: `' + digest(work / 'custody-recovery.json') + '`\n')
        return
    recover_imports(identity, work)
    remote = False
    has_state = False
    if exists('head-bucket', '--bucket', binding['bucket']):
        try:
            secured = secure_backend(binding)
            private_json(TF / 'backend.tf.json', {'terraform': {'backend': {'s3': secured}}})
            run(['terraform', 'init', '-lockfile=readonly', '-input=false', '-no-color'], log=work / 'init.log')
            remote = True
            has_state = exists('head-object', '--bucket', secured['bucket'], '--key', secured['key'])
            require(has_state or (work / 'recovery-input/custody-recovery.json').exists(), 'custody exists without canonical state; reviewed recovery journal required before adopting resources')
        except (ValueError, KeyError):
            require((work / 'recovery-input/custody-recovery.json').exists(), 'partial insecure custody requires its governed recovery journal; no application infrastructure will run')
    if not remote or not has_state:
        if not remote:
            run(['terraform', 'init', '-backend=false', '-lockfile=readonly', '-input=false', '-no-color'], log=work / 'init.log')
        initial, changes, initial_sha = plan(cfg_path, work, custody_only=True)
        private_json(work / 'review.json', {'schema': 'honua.hosted-pilot-bootstrap-plan/v1', **identity, 'phase': 'custody-only', 'resource_changes': changes, 'plan_sha256': digest(initial)})
        if args.mode == 'plan':
            print('Custody-only plan metadata prepared; no binary plan/state/log is uploaded.')
            return
        try:
            exact_apply(initial, initial_sha, work)
            # Preserve an encrypted, versioned recovery state before attempting
            # migration, including when migration itself subsequently fails.
            secured = secure_backend(binding)
            if not remote:
                require(save_recovery(binding, cfg, identity, work), 'custody created but encrypted recovery state persistence failed')
                private_json(TF / 'backend.tf.json', {'terraform': {'backend': {'s3': secured}}})
                run(['terraform', 'init', '-migrate-state', '-force-copy', '-input=false', '-no-color'], log=work / 'migrate.log')
            require(exists('head-object', '--bucket', secured['bucket'], '--key', secured['key']), 'remote foundation state migration not verified')
        except (ValueError, KeyError, OSError):
            save_recovery(binding, cfg, identity, work)
            raise ValueError('initial custody/migration failed; application/identity phase refused. Retain custody-recovery metadata and inspect encrypted recovery custody before retrying') from None
    full, changes, full_sha = plan(cfg_path, work)
    private_json(work / 'review.json', {'schema': 'honua.hosted-pilot-bootstrap-plan/v1', **identity, 'phase': 'remote-backed-foundation', 'resource_changes': changes, 'plan_sha256': digest(full)})
    if args.mode == 'plan':
        print('Remote-backed foundation plan metadata prepared; no infrastructure applied.')
        return
    try:
        plan_sha = exact_apply(full, full_sha, work)
    except ValueError:
        raise ValueError('foundation apply failed; partial resource liability remains in encrypted remote state. Use a fresh source-reviewed plan to reconcile; never discard custody') from None
    outputs = json.loads(run(['terraform', 'output', '-json']))
    private_json(work / 'bindings.json', {'schema': 'honua.hosted-pilot-bootstrap-bindings/v1',
        'release_root_sha': e['PILOT_RELEASE_ROOT_SHA'], 'foundation_source_sha': e['GITHUB_SHA'],
        'foundation_run_id': e['GITHUB_RUN_ID'], 'saved_plan_sha256': plan_sha,
        **{key: outputs[key]['value'] for key in ['deployment_bindings', 'permissions_boundary_arn', 'budget_binding']}})
    with open(e['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write('Foundation bindings SHA256: `' + digest(work / 'bindings.json') + '`\n')
    print('Exact source-reviewed bootstrap plans applied. Full phase is remote-backed; no binary plan/state/log is uploaded.')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f'bootstrap refused: {error}', file=sys.stderr)
        sys.exit(1)
